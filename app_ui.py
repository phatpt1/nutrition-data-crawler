"""Universal Knowledge Crawler — local-first Streamlit application."""
import hashlib
import io
import ipaddress
import json
import re
import socket
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
import trafilatura
import fitz

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
DB = DATA / 'crawler.sqlite3'
USER_AGENT = 'UniversalKnowledgeCrawler/1.0 (respectful research crawler)'
HEADERS = {'User-Agent': USER_AGENT, 'Accept': 'text/html,application/pdf;q=0.9,*/*;q=0.5'}
MAX_BYTES = 12 * 1024 * 1024


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute('CREATE TABLE IF NOT EXISTS topics (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, config TEXT NOT NULL)')
    con.execute('''CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, title TEXT, content TEXT,
        sha256 TEXT, content_type TEXT, fetched_at TEXT)''')
    con.execute('''CREATE TABLE IF NOT EXISTS memberships (
        topic_id INTEGER, document_id INTEGER, accepted INTEGER DEFAULT 0,
        PRIMARY KEY(topic_id, document_id))''')
    con.commit()
    return con


def normalize_url(url):
    url = url.strip()
    if not url: raise ValueError('URL trống')
    if not url.startswith(('https://', 'http://')): url = 'https://' + url
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('URL không hợp lệ')
    if p.port and p.port not in (80, 443): raise ValueError('Chỉ cho phép cổng HTTP/HTTPS tiêu chuẩn')
    return urlunsplit((p.scheme, p.netloc.lower(), p.path or '/', p.query, ''))


def is_public_host(url):
    host = urlsplit(url).hostname
    if not host or host.lower() in ('localhost',) or host.lower().endswith(('.local', '.internal', '.localhost')):
        return False
    try:
        answers = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        return bool(answers) and all(ipaddress.ip_address(item[4][0]).is_global for item in answers)
    except (OSError, ValueError):
        return False


def safe_request(url, timeout=20):
    """Validate each redirect destination; do not automatically follow redirects."""
    current = normalize_url(url)
    with requests.Session() as session:
        session.trust_env = False
        for _ in range(5):
            if not is_public_host(current): raise ValueError('Đích nội bộ/không công khai bị chặn')
            response = session.get(current, headers=HEADERS, timeout=timeout, allow_redirects=False, stream=True)
            if response.status_code in (301, 302, 303, 307, 308):
                nxt = response.headers.get('Location')
                response.close()
                if not nxt: raise ValueError('Redirect thiếu Location')
                current = normalize_url(urljoin(current, nxt))
                continue
            response.raise_for_status()
            size = 0
            chunks = []
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    response.close()
                    raise ValueError('Tài liệu vượt giới hạn 12 MB')
                chunks.append(chunk)
            ctype = response.headers.get('Content-Type', '').lower()
            encoding = response.encoding or 'utf-8'
            response.close()
            return current, b''.join(chunks), ctype, encoding
    raise ValueError('Quá nhiều redirect')


def robots_allowed(url, cache):
    p = urlsplit(url)
    origin = f'{p.scheme}://{p.netloc}'
    if origin not in cache:
        robots_url = origin + '/robots.txt'
        try:
            _, raw, _, enc = safe_request(robots_url, timeout=12)
            rp = RobotFileParser()
            rp.parse(raw.decode(enc, errors='replace').splitlines())
            cache[origin] = rp
        except Exception:
            cache[origin] = None
    rp = cache[origin]
    return rp is not None and rp.can_fetch(USER_AGENT, url)


def extract(url, raw, ctype, encoding):
    if 'pdf' in ctype or urlsplit(url).path.lower().endswith('.pdf'):
        with fitz.open(stream=raw, filetype='pdf') as pdf:
            text = '\n\n'.join(page.get_text(sort=True) for page in pdf[:min(len(pdf), 150)])
        title = urlsplit(url).path.rsplit('/', 1)[-1] or 'PDF'
        return title, text.strip(), 'pdf', []
    if 'html' not in ctype and not raw.lstrip().startswith((b'<!DOCTYPE', b'<html', b'<HTML')):
        raise ValueError('Chỉ hỗ trợ HTML/PDF')
    html = raw.decode(encoding, errors='replace')
    soup = BeautifulSoup(html, 'html.parser')
    title = (soup.h1.get_text(' ', strip=True) if soup.h1 else (soup.title.get_text(' ', strip=True) if soup.title else url))
    links = [urljoin(url, a['href']) for a in soup.select('a[href]')]
    text = trafilatura.extract(html, url=url, output_format='markdown', include_tables=True, include_links=False)
    if not text:
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']): tag.decompose()
        text = (soup.find('main') or soup.find('article') or soup).get_text('\n', strip=True)
    return title.strip(), (text or '').strip(), 'html', links


def filter_reason(title, content, cfg):
    if len(content) < int(cfg['min_chars']): return 'Nội dung quá ngắn'
    text = (title + '\n' + content).casefold()
    exclusions = [s.casefold().strip() for s in cfg['exclude'] if s.strip()]
    if any(s in text for s in exclusions): return 'Khớp từ khóa loại trừ'
    inclusions = [s.casefold().strip() for s in cfg['include'] if s.strip()]
    if inclusions and not any(s in text for s in inclusions): return 'Không khớp từ khóa topic'
    return ''


def crawl(seeds, cfg, on_log):
    queue = [(normalize_url(u), 0) for u in seeds]
    allowed_hosts = {urlsplit(u).hostname for u, _ in queue}
    visited = set()
    robots = {}
    accepted, rejected = 0, 0
    con = db()
    try:
        while queue and len(visited) < cfg['max_pages']:
            url, depth = queue.pop(0)
            if url in visited: continue
            visited.add(url)
            if urlsplit(url).hostname not in allowed_hosts:
                on_log(f'BỎ QUA (ngoài domain): {url}')
                continue
            if not robots_allowed(url, robots):
                on_log(f'BỎ QUA (robots.txt không cho phép hoặc không kiểm tra được): {url}')
                rejected += 1
                continue
            try:
                final_url, raw, ctype, enc = safe_request(url)
                if urlsplit(final_url).hostname not in allowed_hosts:
                    raise ValueError('Redirect ra ngoài domain được chọn')
                title, content, kind, links = extract(final_url, raw, ctype, enc)
                reason = filter_reason(title, content, cfg)
                if reason:
                    rejected += 1
                    on_log(f'LOẠI: {title[:60]} — {reason}')
                else:
                    digest = hashlib.sha256(content.encode('utf-8')).hexdigest()
                    con.execute('''INSERT INTO documents(url,title,content,sha256,content_type,fetched_at)
                      VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                      content=excluded.content,sha256=excluded.sha256,content_type=excluded.content_type,
                      fetched_at=excluded.fetched_at''',
                      (final_url, title, content, digest, kind, datetime.now(timezone.utc).isoformat()))
                    doc_id = con.execute('SELECT id FROM documents WHERE url=?', (final_url,)).fetchone()['id']
                    con.execute('INSERT OR IGNORE INTO memberships(topic_id,document_id,accepted) VALUES(?,?,0)', (cfg['topic_id'], doc_id))
                    con.commit()
                    accepted += 1
                    on_log(f'ĐÃ LƯU: {title[:75]}')
                if depth < cfg['max_depth']:
                    for link in links:
                        try:
                            link = normalize_url(link)
                            if urlsplit(link).hostname in allowed_hosts and link not in visited and not re.search(r'\.(jpg|jpeg|png|gif|zip|exe|css|js|svg)(\?|$)', link, re.I):
                                if len(queue) < cfg['max_pages'] * 5: queue.append((link, depth + 1))
                        except ValueError:
                            pass
            except Exception as exc:
                rejected += 1
                on_log(f'LỖI: {url[:90]} — {exc}')
            time.sleep(cfg['delay'])
    finally:
        con.close()
    return accepted, rejected, len(visited)


def rows_for_topic(con, topic_id):
    return con.execute('''SELECT d.*,m.accepted FROM documents d JOIN memberships m ON m.document_id=d.id
                          WHERE m.topic_id=? ORDER BY d.fetched_at DESC''', (topic_id,)).fetchall()


def export_zip(rows):
    out = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            slug = re.sub(r'[^a-zA-Z0-9_-]+', '-', row['title'])[:50].strip('-') or 'document'
            filename = f"{row['id']:05d}-{slug}.md"
            body = f"# {row['title']}\n\nSource: {row['url']}\nFetched: {row['fetched_at']}\n\n{row['content']}\n"
            archive.writestr('documents/' + filename, body)
            manifest.append({'file': filename, 'url': row['url'], 'title': row['title'], 'sha256': row['sha256']})
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    return out.getvalue()

st.set_page_config(page_title='Universal Knowledge Crawler', page_icon='📚', layout='wide')
st.title('📚 Universal Knowledge Crawler')
st.caption('Crawl đa lĩnh vực · Lưu trữ local · Duyệt tài liệu · Xuất Markdown cho RAG')
con = db()
tab_topic, tab_crawl, tab_library, tab_sync = st.tabs(['🗂️ Topics', '🌐 Crawl', '📖 Library', '🔗 OpenWebUI'])

with tab_topic:
    st.subheader('Tạo topic mới')
    name = st.text_input('Tên topic', placeholder='Network Security / Accounting / Machine Learning / General')
    a, b = st.columns(2)
    with a:
        include = st.text_area('Từ khóa liên quan (mỗi dòng một từ, để trống = không lọc)')
    with b:
        exclude = st.text_area('Từ khóa loại trừ (mỗi dòng một từ)')
    if st.button('Tạo topic', type='primary'):
        if not name.strip(): st.error('Nhập tên topic')
        else:
            try:
                con.execute('INSERT INTO topics(name,config) VALUES(?,?)', (name.strip(), json.dumps({'include': include.splitlines(), 'exclude': exclude.splitlines()}, ensure_ascii=False)))
                con.commit()
                st.success('Đã tạo topic')
            except sqlite3.IntegrityError: st.warning('Topic đã tồn tại')
    topics = con.execute('SELECT * FROM topics ORDER BY name').fetchall()
    if topics: st.dataframe(pd.DataFrame([{'ID': t['id'], 'Topic': t['name'], 'Keywords': ', '.join(json.loads(t['config']).get('include', []))} for t in topics]), hide_index=True)

with tab_crawl:
    topics = con.execute('SELECT * FROM topics ORDER BY name').fetchall()
    if not topics:
        st.info('Tạo topic ở tab Topics trước. Có thể đặt tên General và không nhập từ khóa.')
    else:
        options = {f"{t['name']} (#{t['id']})": t for t in topics}
        selected = options[st.selectbox('Topic đích', list(options))]
        cfg_saved = json.loads(selected['config'])
        urls = st.text_area('Seed URLs (mỗi dòng một URL)', placeholder='https://learn.microsoft.com/...\nhttps://csrc.nist.gov/...', height=110)
        c1, c2, c3 = st.columns(3)
        with c1: max_depth = st.slider('Độ sâu link', 0, 3, 1)
        with c2: max_pages = st.number_input('Tối đa trang / lần', 1, 200, 20)
        with c3: delay = st.number_input('Chờ giữa các trang (giây)', 1.0, 30.0, 2.0, 0.5)
        min_chars = st.number_input('Số ký tự tối thiểu', 100, 10000, 250, 50)
        st.caption('Chỉ theo domain seed URL; robots.txt không đọc được sẽ bỏ qua. Không truy cập host/IP nội bộ. Chỉ crawl nguồn được phép.')
        if st.button('🚀 Bắt đầu crawl', type='primary'):
            seeds = [x.strip() for x in urls.splitlines() if x.strip()]
            if not seeds: st.error('Nhập ít nhất một URL')
            else:
                cfg = dict(cfg_saved, topic_id=selected['id'], max_depth=max_depth, max_pages=int(max_pages), delay=float(delay), min_chars=int(min_chars))
                logs = []
                placeholder = st.empty()
                def log(message):
                    logs.append(message)
                    placeholder.code('\n'.join(logs[-20:]), language='text')
                try:
                    ok, bad, count = crawl(seeds, cfg, log)
                    st.success(f'Đã duyệt {count} URL · Lưu {ok} · Bỏ qua/lỗi {bad}')
                except Exception as exc: st.error(str(exc))

with tab_library:
    topics = con.execute('SELECT * FROM topics ORDER BY name').fetchall()
    if not topics: st.info('Chưa có topic')
    else:
        selected = st.selectbox('Xem topic', topics, format_func=lambda x:x['name'], key='library_topic')
        rows = rows_for_topic(con, selected['id'])
        st.metric('Tài liệu', len(rows))
        if rows:
            st.dataframe(pd.DataFrame([{'ID': r['id'], 'Title': r['title'], 'Source': r['url'], 'Approved': bool(r['accepted']), 'Fetched': r['fetched_at']} for r in rows]), hide_index=True)
            chosen = st.selectbox('Xem và duyệt tài liệu', rows, format_func=lambda r:f"#{r['id']} · {r['title']}")
            st.markdown(f"**Nguồn:** {chosen['url']}")
            st.text_area('Nội dung trích xuất', chosen['content'], height=270)
            approved = st.checkbox('Duyệt tài liệu này để xuất KB', value=bool(chosen['accepted']), key=f"approve_{chosen['id']}_{selected['id']}")
            if st.button('Lưu trạng thái duyệt'):
                con.execute('UPDATE memberships SET accepted=? WHERE topic_id=? AND document_id=?', (int(approved), selected['id'], chosen['id']))
                con.commit()
                st.success('Đã lưu')
            approved_rows = [r for r in rows if r['accepted']]
            st.download_button('📦 Tải ZIP Markdown + manifest (đã duyệt)', export_zip(approved_rows), file_name=f"topic-{selected['id']}-knowledge.zip", mime='application/zip', disabled=not approved_rows)
            st.download_button('📋 Tải JSON (đã duyệt)', json.dumps([dict(r) for r in approved_rows], ensure_ascii=False, indent=2), file_name=f"topic-{selected['id']}-knowledge.json", mime='application/json', disabled=not approved_rows)

with tab_sync:
    st.subheader('OpenWebUI connector')
    st.info('V1: xuất Markdown/JSON đã kiểm duyệt. V2: kết nối API OpenWebUI để upload và gắn KB; chưa tự động đồng bộ ở phiên bản này.')
    st.markdown('1. Vào **Library** → duyệt tài liệu.\n2. Tải ZIP Markdown.\n3. Giải nén, vào **OpenWebUI → Workspace → Knowledge** để upload các file Markdown.')
    st.warning('Không nhập Admin API key vào source code hoặc file JSON xuất ra. Cloudflare Access có thể yêu cầu Service Token riêng.')
con.close()
