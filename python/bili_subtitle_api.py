"""Fetch Bilibili AI subtitles via player API with cookie auth."""
import requests, json, re, time

COOKIE_FILE = 'D:\\Agent\\B站视频总结\\cookies_www.bilibili.com.txt'

def load_netscape_cookies(path):
    """Parse Netscape format cookies.txt into a dict."""
    cookies = {}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
    except FileNotFoundError:
        pass
    return cookies

def extract_bvid(url):
    """Extract BV id from Bilibili URL."""
    m = re.search(r'[bB][Vv][0-9A-Za-z]+', url)
    return m.group(0) if m else None

def get_ai_subtitle(url_or_bvid, cookie_file=""):
    """Download AI subtitle from Bilibili player API.
    Falls back to COOKIE_FILE if cookie_file is empty.
    Returns (text, lines) or (None, None) on failure."""
    bvid = extract_bvid(url_or_bvid)
    if not bvid:
        return None, None
    
    if not cookie_file:
        cookie_file = COOKIE_FILE
    cookies = load_netscape_cookies(cookie_file)
    if 'SESSDATA' not in cookies:
        return None, None
    
    headers = {
        'Referer': 'https://www.bilibili.com',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    
    try:
        # Step 1: get cid and aid
        resp = requests.get(
            f'https://api.bilibili.com/x/web-interface/view?bvid={bvid}',
            cookies=cookies, headers=headers, timeout=10
        )
        data = resp.json()
        if data.get('code') != 0:
            return None, None
        cid = data['data']['cid']
        aid = data['data']['aid']
        
        # Step 2: get subtitle URL from player API
        resp2 = requests.get(
            f'https://api.bilibili.com/x/player/v2?cid={cid}&aid={aid}&bvid={bvid}',
            cookies=cookies, headers=headers, timeout=10
        )
        player_data = resp2.json()
        if player_data.get('code') != 0:
            return None, None
        
        subs = player_data.get('data', {}).get('subtitle', {}).get('subtitles', [])
        if not subs:
            return None, None
        
        # Pick ai-zh or first available
        sub_url = None
        for s in subs:
            if s.get('lan') == 'ai-zh' or s.get('lan', '').startswith('ai-'):
                sub_url = s.get('subtitle_url')
                break
        if not sub_url:
            sub_url = subs[0].get('subtitle_url')
        if not sub_url:
            return None, None
        
        # Step 3: download subtitle JSON
        full_url = 'https:' + sub_url if sub_url.startswith('//') else sub_url
        sub_resp = requests.get(full_url, timeout=10)
        sub_data = sub_resp.json()
        body = sub_data.get('body', [])
        if not body:
            return None, None
        
        # Build text
        text = ' '.join(b['content'] for b in body)
        return text, body
        
    except Exception:
        return None, None

def convert_body_to_srt(body, output_path):
    """Convert subtitle body list to SRT file."""
    lines = []
    for i, b in enumerate(body, 1):
        start = _fmt_time(b['from'])
        end = _fmt_time(b['to'])
        lines.append(f"{i}")
        lines.append(f"{start} --> {end}")
        lines.append(b['content'])
        lines.append("")
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

def _fmt_time(seconds):
    """Convert seconds to SRT time format."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace('.', ',')

if __name__ == '__main__':
    text, body = get_ai_subtitle('https://www.bilibili.com/video/BV1WwVz6HE7j')
    if text:
        print(f"SUCCESS: {len(body)} lines, {len(text)} chars")
        print(text[:200])
    else:
        print("FAILED")
