import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import streamlit as st

st.set_page_config(page_title="Data Crawler Dinh Dưỡng Nâng Cao", page_icon="🥗", layout="wide")
st.title("🥗 Công Cụ Cào Sâu & Lọc Kỹ Dữ Liệu Dinh Dưỡng / Calo")

DOWNLOADS_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_CLEAN_JSON = os.path.join(DOWNLOADS_DIR, "dataset_dinh_duong_CUSTOM.json")

# Danh sách từ khóa bắt buộc
MUST_HAVE_KEYWORDS = [
    "dinh dưỡng", "calo", "calorie", "protein", "carb", "chất béo", "chất xơ",
    "thực đơn", "ăn uống", "tăng cân", "giảm cân", "béo phì", "tiểu đường",
    "mỡ máu", "gout", "axit uric", "tim mạch", "huyết áp", "gan nhiễm mỡ",
    "vitamin", "khoáng chất", "ăn kiêng", "keto", "intermittent fasting", "nhịn ăn", 
    "thực phẩm", "món ăn", "suy nhược", "bồi bổ", "món ăn bài thuốc", "chế độ ăn"
]

# Danh sách từ khóa loại trừ bài rác
EXCLUDE_KEYWORDS = [
    "bản quyền", "đặt lịch khám", "tuyển dụng", "bằng khen",
    "liên hệ", "sơ đồ trang", "lịch công tác", "đăng nhập", "chính sách bảo mật"
]

def fix_url_format(url):
    """Đảm bảo URL luôn có https://"""
    url = url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return url

def clean_text(text):
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def is_nutrition_topic(title, content):
    full_text = (title + " " + content).lower()
    for exc in EXCLUDE_KEYWORDS:
        if exc in title.lower():
            return False, f"Chứa từ khóa loại trừ: '{exc}'"
            
    score = sum(1 for kw in MUST_HAVE_KEYWORDS if kw in full_text)
    if score >= 1:
        return True, f"Đạt chuẩn (Khớp {score} từ khóa chuyên ngành)"
    return False, f"Không chứa từ khóa dinh dưỡng/sức khỏe (Score = {score})"

def get_sub_links(base_url, max_links=30):
    """Quét trang gốc để lấy danh sách các URL bài viết con"""
    base_url = fix_url_format(base_url)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    sub_links = set()
    try:
        res = requests.get(base_url, headers=headers, timeout=10)
        res.encoding = 'utf-8'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        domain = urlparse(base_url).netloc
        
        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            full_url = urljoin(base_url, href)
            parsed_url = urlparse(full_url)
            
            # Chỉ lấy các link cùng domain và không chứa file tĩnh/anchor
            if parsed_url.netloc == domain and not any(full_url.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.pdf', '.docx', '#']):
                if not any(x in full_url.lower() for x in ['login', 'register', 'contact', 'lien-he', 'cart', 'search']):
                    sub_links.add(full_url)
                    if len(sub_links) >= max_links:
                        break
    except Exception as e:
        st.error(f"Lỗi khi tìm link con từ {base_url}: {e}")
        
    return list(sub_links)

def scrape_single_article(url):
    url = fix_url_format(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        res = requests.get(url, headers=headers, timeout=12)
        res.encoding = 'utf-8'
        soup = BeautifulSoup(res.text, 'html.parser')
        
        for tag in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe', 'noscript']):
            tag.decompose()
            
        title = ""
        if soup.find('h1'):
            title = soup.find('h1').get_text()
        elif soup.title:
            title = soup.title.get_text()
        title = clean_text(title)
        
        paragraphs = []
        main_content = (
            soup.find('article') 
            or soup.find('div', class_=re.compile(r'entry-content|post-content|td-post-content|content-detail|vc_column-inner', re.I))
            or soup.find('main') 
            or soup
        )
        
        for el in main_content.find_all(['p', 'li', 'h2', 'h3']):
            text = clean_text(el.get_text())
            if len(text) > 25 and not any(kw in text.lower() for kw in EXCLUDE_KEYWORDS):
                paragraphs.append(text)
                
        content = "\n".join(paragraphs)
        
        if len(content) < 150:
            return None, f"Nội dung cào được quá ngắn ({len(content)} ký tự)"
            
        is_valid, reason = is_nutrition_topic(title, content)
        if not is_valid:
            return None, reason
            
        return {"url": url, "title": title, "content": content}, "Thành công"
    except Exception as e:
        return None, f"Lỗi truy cập: {e}"

# --- GIAO DIỆN STREAMLIT ---
st.markdown("### 🎯 Nhập URL (Trang chủ / Chuyên mục / Bài viết)")
urls_input = st.text_area("Dán các đường dẫn vào đây (Mỗi URL 1 dòng):", height=100, 
                          placeholder="tamanhhospital.vn/mon-an-cho-nguoi-suy-nhuoc-co-the/\nhttps://viendinhduong.vn/")

col1, col2 = st.columns(2)
with col1:
    deep_crawl = st.checkbox("🔍 Bật chế độ CÀO SÂU (Tự động tìm & cào tất cả bài viết bên trong trang gốc)", value=False)
with col2:
    max_sub_links = st.slider("Số bài viết con tối đa muốn quét cho mỗi trang:", min_value=5, max_value=100, value=20)

if st.button("🚀 Bắt đầu Cào & Lọc Dữ Liệu", type="primary"):
    raw_urls = [u.strip() for u in urls_input.split("\n") if u.strip()]
    if not raw_urls:
        st.warning("Vui lòng nhập ít nhất 1 URL!")
    else:
        results = []
        if os.path.exists(OUTPUT_CLEAN_JSON):
            try:
                with open(OUTPUT_CLEAN_JSON, "r", encoding="utf-8") as f:
                    results = json.load(f)
            except: results = []
            
        existing_urls = {item["url"] for item in results if isinstance(item, dict) and "url" in item}
        
        target_urls = []
        status_box = st.info("Đang thu thập danh sách liên kết...")
        
        for url in raw_urls:
            url_formatted = fix_url_format(url)
            if deep_crawl:
                st.write(f"🔍 Đang tìm các bài viết bên trong: `{url_formatted}`...")
                found_links = get_sub_links(url_formatted, max_links=max_sub_links)
                st.write(f"   ↳ Tìm thấy **{len(found_links)}** liên kết tiềm năng.")
                target_urls.extend(found_links)
            target_urls.append(url_formatted)
            
        target_urls = list(set(target_urls))
        status_box.empty()
        
        st.write(f"📋 **Tổng số {len(target_urls)} bài viết sẽ được kiểm tra và cào dữ liệu:**")
        
        progress_bar = st.progress(0)
        log_container = st.container()
        
        added_count = 0
        for idx, url in enumerate(target_urls):
            if url in existing_urls:
                with log_container:
                    st.text(f"⏩ [{idx+1}/{len(target_urls)}] Đã có trong DB, bỏ qua: {url[:60]}...")
                progress_bar.progress((idx + 1) / len(target_urls))
                continue
                
            data, status_msg = scrape_single_article(url)
            
            with log_container:
                if data:
                    results.append(data)
                    existing_urls.add(url)
                    added_count += 1
                    st.success(f"✅ [{idx+1}/{len(target_urls)}] ACCEPT: {data['title'][:50]}... ({status_msg})")
                else:
                    st.caption(f"🛑 [{idx+1}/{len(target_urls)}] REJECT: {url[:50]}... -> {status_msg}")
                    
            progress_bar.progress((idx + 1) / len(target_urls))
            time.sleep(0.3)
            
        with open(OUTPUT_CLEAN_JSON, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
            
        st.balloons()
        st.success(f"🎉 HOÀN THÀNH! Đã thêm thành công {added_count} bài viết đạt chuẩn vào file JSON.")