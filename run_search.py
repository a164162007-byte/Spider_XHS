"""
定时爬取小红书关键词最新笔记
支持多关键词、自定义间隔轮爬、自动去重、Web扫码登录

Docker 使用方式：
1. 定时爬取模式（Web扫码登录，推荐）：
   docker run -d \
     -p 5000:5000 \
     -e SEARCH_KEYWORDS='榴莲,芒果' \
     -e SEARCH_NUM=20 \
     -e INTERVAL_MINUTES=30 \
     -e ROUND_INTERVAL_HOURS=4 \
     -v /你的路径/datas:/app/datas \
     ghcr.io/a164162007-byte/spider_xhs:latest \
     python run_search.py

2. 使用已有cookie（无需扫码）：
   docker run -d \
     -e LOGIN_MODE=cookie \
     -e COOKIES='你的cookie字符串' \
     -e SEARCH_KEYWORDS='榴莲,芒果' \
     -v /你的路径/datas:/app/datas \
     ghcr.io/a164162007-byte/spider_xhs:latest \
     python run_search.py

首次使用：浏览器打开 http://群晖IP:5000 扫码登录，成功后cookie自动保存
"""
import os
import sys
import time
import json
import random
import threading
import qrcode
from io import BytesIO
from loguru import logger
from apis.xhs_pc_apis import XHS_Apis
from apis.xhs_pc_login_apis import XHSLoginApi
from xhs_utils.common_util import init
from xhs_utils.data_util import handle_note_info, download_note, save_to_xlsx

# ========== 环境变量配置 ==========
# LOGIN_MODE: 登录方式 qrcode=扫码登录(默认) / cookie=手动填cookie
# COOKIES: 手动填cookie时使用，LOGIN_MODE=cookie时必填
# SEARCH_KEYWORDS: 关键词列表，逗号分隔，如 "榴莲,芒果,椰子"
# SEARCH_NUM: 每个关键词每次爬取数量，默认 20
# INTERVAL_MINUTES: 每个关键词之间的间隔（分钟），默认 30
# ROUND_INTERVAL_HOURS: 每轮（所有关键词爬完一遍）后的间隔（小时），默认 4
# SAVE_CHOICE: 保存方式 all/media/excel/media-image/media-video，默认 all
# SORT_TYPE: 排序 0综合/1最新/2最多点赞/3最多评论/4最多收藏，默认 1（最新）
# NOTE_TYPE: 笔记类型 0不限/1视频/2图文，默认 0
# NOTE_TIME: 时间范围 0不限/1一天内/2一周内/3半年内，默认 1（一天内）
# WEB_PORT: Web扫码登录端口，默认 5000

# 数据目录（Docker 内路径，需挂载到宿主机持久化）
DATAS_DIR = '/app/datas'
COOKIE_FILE = os.path.join(DATAS_DIR, '.saved_cookies.json')
QRCODE_IMAGE = os.path.join(DATAS_DIR, 'qrcode_login.png')

# ========== 全局状态 ==========
login_state = {
    'status': 'waiting',       # waiting / scanning / success / failed / expired
    'qr_url': None,            # 二维码链接
    'qr_image_b64': None,      # 二维码图片 base64
    'cookies_str': None,       # 登录成功后的 cookie
    'user_info': None,         # 用户信息
    'message': '等待登录...',
}
login_lock = threading.Lock()


def load_config():
    """从环境变量加载配置"""
    keywords_str = os.getenv('SEARCH_KEYWORDS', '')
    if not keywords_str:
        raise ValueError("必须设置环境变量 SEARCH_KEYWORDS，多个关键词用逗号分隔")

    keywords = [k.strip() for k in keywords_str.split(',') if k.strip()]
    if not keywords:
        raise ValueError("SEARCH_KEYWORDS 不能为空")

    config = {
        'login_mode': os.getenv('LOGIN_MODE', 'qrcode'),
        'keywords': keywords,
        'search_num': int(os.getenv('SEARCH_NUM', '20')),
        'interval_minutes': int(os.getenv('INTERVAL_MINUTES', '30')),
        'round_interval_hours': float(os.getenv('ROUND_INTERVAL_HOURS', '4')),
        'save_choice': os.getenv('SAVE_CHOICE', 'all'),
        'sort_type': int(os.getenv('SORT_TYPE', '1')),
        'note_type': int(os.getenv('NOTE_TYPE', '0')),
        'note_time': int(os.getenv('NOTE_TIME', '1')),
        'web_port': int(os.getenv('WEB_PORT', '5000')),
    }
    return config


def load_saved_cookies():
    """从文件加载已保存的cookie"""
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                cookies_str = data.get('cookies', '')
                if cookies_str:
                    login_time = data.get('login_time', '未知')
                    logger.info(f"已从文件加载保存的cookie（上次登录: {login_time}）")
                    return cookies_str
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"读取保存的cookie失败: {e}")
    return None


def save_cookies_to_file(cookies_str):
    """保存cookie到文件，下次启动自动使用"""
    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    from datetime import datetime
    data = {
        'cookies': cookies_str,
        'login_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("Cookie已保存到文件，下次启动自动使用")


def validate_cookies(cookies_str):
    """验证cookie是否有效"""
    logger.info("正在验证Cookie有效性...")
    xhs_apis = XHS_Apis()
    try:
        success, msg, notes = xhs_apis.search_some_note(
            query="test",
            require_num=1,
            cookies_str=cookies_str,
            sort_type_choice=1,
            note_type=0,
            note_time=0,
            note_range=0,
            pos_distance=0,
            geo=None,
            proxies=None
        )
        if success:
            logger.success("Cookie验证通过 ✓")
            return True
        else:
            logger.warning(f"Cookie验证失败: {msg}")
            return False
    except Exception as e:
        error_msg = str(e)
        if 'NoneType' in error_msg:
            logger.warning("Cookie已失效（API返回空数据）")
        else:
            logger.warning(f"Cookie验证异常: {e}")
        return False


def do_qrcode_login_web():
    """
    Web版扫码登录流程
    1. 生成二维码，存入全局状态供网页展示
    2. 启动Flask服务让用户浏览器访问
    3. 轮询等待扫码结果
    """
    import base64
    from flask import Flask, Response

    login_api = XHSLoginApi()

    # 步骤1：生成初始 cookies
    logger.info('[1/4] 正在生成初始cookies...')
    cookies = login_api.generate_init_cookies()

    # 步骤2：获取二维码
    logger.info('[2/4] 正在获取二维码...')
    success, msg, qr_data = login_api.generate_qrcode(cookies)
    if not success:
        logger.error(f'获取二维码失败: {msg}')
        with login_lock:
            login_state['status'] = 'failed'
            login_state['message'] = f'获取二维码失败: {msg}'
        return None
    cookies = qr_data['cookies']

    # 生成二维码图片 base64
    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(qr_data['qr_url'])
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    # 保存图片文件到挂载目录
    os.makedirs(DATAS_DIR, exist_ok=True)
    img.save(QRCODE_IMAGE)
    logger.info(f"二维码图片已保存: {QRCODE_IMAGE}")

    # 转 base64 给网页
    buf = BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    with login_lock:
        login_state['status'] = 'scanning'
        login_state['qr_url'] = qr_data['qr_url']
        login_state['qr_image_b64'] = qr_b64
        login_state['message'] = '请使用小红书APP扫描二维码'

    # 同时在终端打印
    try:
        qr_term = qrcode.QRCode(box_size=1, border=1)
        qr_term.add_data(qr_data['qr_url'])
        qr_term.make(fit=True)
        qr_term.print_ascii(invert=True)
    except Exception:
        pass

    # 步骤3：等待扫码
    logger.info('[3/4] 等待扫码（浏览器打开 http://群晖IP:5000 查看二维码）...')
    while True:
        success, msg, cookies = login_api.check_qrcode_status(
            qr_data['qr_id'], qr_data['code'], cookies
        )
        if success:
            logger.info(msg)
            break
        if msg == '二维码已过期':
            logger.error('二维码已过期')
            with login_lock:
                login_state['status'] = 'expired'
                login_state['message'] = '二维码已过期，请刷新页面重新获取'
            return None
        time.sleep(2)

    # 步骤4：验证登录状态
    logger.info('[4/4] 验证登录状态...')
    success, user_info, cookies = login_api.get_user_info(cookies)
    nickname = user_info.get('nickname', '未知') if success else '未知'
    red_id = user_info.get('red_id', '未知') if success else '未知'

    cookies_str = login_api.cookies_to_str(cookies)

    with login_lock:
        login_state['status'] = 'success'
        login_state['cookies_str'] = cookies_str
        login_state['user_info'] = {'nickname': nickname, 'red_id': red_id}
        login_state['message'] = f'登录成功！用户: {nickname}'

    logger.success(f'扫码登录成功！用户: {nickname} (RedId: {red_id})')
    return cookies_str


def start_web_server(port):
    """启动Flask Web服务，提供扫码登录页面"""
    from flask import Flask, Response, jsonify

    app = Flask(__name__)

    @app.route('/')
    def index():
        html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小红书爬虫 - 扫码登录</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #0D1117; color: #c9d1d9; display: flex; justify-content: center;
       align-items: center; min-height: 100vh; }
.card { background: #161B22; border: 1px solid #30363D; border-radius: 12px;
        padding: 40px; text-align: center; max-width: 420px; width: 90%; }
h1 { color: #FF2D2D; font-size: 22px; margin-bottom: 8px; }
.subtitle { color: #8b949e; font-size: 14px; margin-bottom: 24px; }
#qr-area { margin: 20px 0; min-height: 200px; display: flex; justify-content: center;
           align-items: center; flex-direction: column; }
#qr-image { border-radius: 8px; max-width: 280px; }
.status { padding: 10px 16px; border-radius: 8px; margin: 16px 0;
          font-size: 14px; font-weight: 500; }
.status-waiting { background: #1a1e24; color: #8b949e; }
.status-scanning { background: #0d2137; color: #58A6FF; }
.status-success { background: #0d2818; color: #3FB950; }
.status-failed { background: #2d0f0f; color: #FF2D2D; }
.status-expired { background: #2d1f0f; color: #D29922; }
.tips { color: #8b949e; font-size: 12px; margin-top: 12px; line-height: 1.6; }
.tips b { color: #c9d1d9; }
.btn { padding: 10px 24px; border-radius: 8px; border: none; cursor: pointer;
       font-size: 14px; font-weight: 500; margin-top: 12px; }
.btn-primary { background: #FF2D2D; color: white; }
.btn-primary:hover { background: #E02020; }
.spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid #58A6FF;
           border-top-color: transparent; border-radius: 50%; animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<div class="card">
  <h1>📕 小红书爬虫</h1>
  <p class="subtitle">扫码登录后自动开始爬取</p>
  <div id="qr-area">
    <div id="qr-content">
      <div class="spinner"></div>
      <p style="margin-top:12px;color:#8b949e">正在生成二维码...</p>
    </div>
  </div>
  <div id="status-bar" class="status status-waiting">等待登录...</div>
  <div class="tips">
    <b>使用方法：</b><br>
    1. 打开小红书APP → 搜索框旁的扫一扫<br>
    2. 扫描上方二维码 → 确认登录<br>
    3. 登录成功后自动开始爬取任务
  </div>
</div>
<script>
let checkTimer = null;
function checkStatus() {
  fetch('/api/status')
    .then(r => r.json())
    .then(data => {
      const qrContent = document.getElementById('qr-content');
      const statusBar = document.getElementById('status-bar');
      // 显示二维码
      if (data.qr_image_b64 && data.status !== 'success') {
        qrContent.innerHTML = '<img id="qr-image" src="data:image/png;base64,' + data.qr_image_b64 + '" />';
      }
      // 状态
      statusBar.className = 'status status-' + data.status;
      statusBar.textContent = data.message;
      if (data.status === 'success') {
        qrContent.innerHTML = '<div style="font-size:48px">✅</div>' +
          '<p style="margin-top:12px;color:#3FB950;font-size:16px">' + data.message + '</p>';
        if (checkTimer) clearInterval(checkTimer);
        setTimeout(() => {
          statusBar.textContent = '爬虫正在后台运行中...';
        }, 2000);
      } else if (data.status === 'expired') {
        qrContent.innerHTML = '<div style="font-size:48px">⏰</div>' +
          '<p style="margin-top:12px;color:#D29922">二维码已过期</p>' +
          '<button class="btn btn-primary" onclick="refreshQR()">刷新二维码</button>';
        if (checkTimer) clearInterval(checkTimer);
      } else if (data.status === 'failed') {
        if (checkTimer) clearInterval(checkTimer);
      }
    })
    .catch(err => console.error(err));
}
function refreshQR() {
  fetch('/api/refresh').then(() => {
    document.getElementById('qr-content').innerHTML = '<div class="spinner"></div><p style="margin-top:12px;color:#8b949e">正在生成二维码...</p>';
    document.getElementById('status-bar').className = 'status status-waiting';
    document.getElementById('status-bar').textContent = '等待登录...';
    if (checkTimer) clearInterval(checkTimer);
    checkTimer = setInterval(checkStatus, 2000);
  });
}
// 启动轮询
checkTimer = setInterval(checkStatus, 2000);
checkStatus();
</script>
</body>
</html>'''
        return Response(html, content_type='text/html; charset=utf-8')

    @app.route('/api/status')
    def api_status():
        with login_lock:
            return jsonify({
                'status': login_state['status'],
                'message': login_state['message'],
                'qr_image_b64': login_state.get('qr_image_b64'),
                'user_info': login_state.get('user_info'),
            })

    @app.route('/api/refresh')
    def api_refresh():
        # 重置状态，让登录线程重新生成二维码
        with login_lock:
            login_state['status'] = 'waiting'
            login_state['qr_image_b64'] = None
            login_state['message'] = '正在重新生成二维码...'
        return jsonify({'ok': True})

    @app.route('/qrcode.png')
    def qrcode_image():
        """直接提供二维码图片文件"""
        if os.path.exists(QRCODE_IMAGE):
            with open(QRCODE_IMAGE, 'rb') as f:
                return Response(f.read(), content_type='image/png')
        return Response('Not found', status=404)

    # Flask 在后台线程运行
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)


def get_cookies(config):
    """获取cookie：优先用保存的 → Web扫码登录 → 手动填的"""
    # 1. 先看有没有已保存的cookie
    saved = load_saved_cookies()
    if saved:
        logger.info("发现已保存的cookie，正在验证...")
        if validate_cookies(saved):
            with login_lock:
                login_state['status'] = 'success'
                login_state['cookies_str'] = saved
                login_state['message'] = '使用已保存的Cookie'
            return saved
        else:
            logger.warning("已保存的cookie已失效，需要重新登录")
            try:
                os.remove(COOKIE_FILE)
            except OSError:
                pass

    # 2. 扫码登录模式
    if config['login_mode'] == 'qrcode':
        logger.info("启动Web扫码登录服务...")
        # 启动Web服务器（后台线程）
        web_thread = threading.Thread(
            target=start_web_server,
            args=(config['web_port'],),
            daemon=True
        )
        web_thread.start()
        logger.info(f"Web登录页面已启动: http://0.0.0.0:{config['web_port']}")

        # 执行扫码登录（会阻塞直到登录成功或失败）
        cookies_str = do_qrcode_login_web()
        if cookies_str:
            save_cookies_to_file(cookies_str)
            logger.success("扫码登录成功！Cookie已自动保存，下次无需再扫")
            return cookies_str
        else:
            # 登录失败，保持Web服务器运行等待刷新重试
            logger.error("扫码登录失败，请在浏览器中刷新重试")
            # 等待用户刷新二维码重试
            for attempt in range(3):
                time.sleep(5)
                with login_lock:
                    if login_state['status'] == 'waiting':
                        cookies_str = do_qrcode_login_web()
                        if cookies_str:
                            save_cookies_to_file(cookies_str)
                            return cookies_str
            logger.error("多次扫码登录失败，程序退出")
            return None

    # 3. 手动cookie模式
    cookies_str, _ = init()
    if not cookies_str:
        logger.error("COOKIES 未配置，请设置环境变量 COOKIES 或使用 LOGIN_MODE=qrcode 扫码登录")
        return None

    # 验证手动填的cookie
    if validate_cookies(cookies_str):
        save_cookies_to_file(cookies_str)
        return cookies_str
    else:
        logger.error("手动填写的Cookie已失效，请重新获取")
        return None


def load_crawled_ids(record_path):
    """加载已爬取的笔记ID集合，用于去重"""
    if os.path.exists(record_path):
        try:
            with open(record_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get('crawled_ids', []))
        except (json.JSONDecodeError, IOError):
            return set()
    return set()


def save_crawled_ids(record_path, crawled_ids):
    """保存已爬取的笔记ID"""
    os.makedirs(os.path.dirname(record_path), exist_ok=True)
    with open(record_path, 'w', encoding='utf-8') as f:
        json.dump({'crawled_ids': list(crawled_ids)}, f, ensure_ascii=False, indent=2)


def crawl_keyword(xhs_apis, keyword, config, cookies_str, base_path, crawled_ids, record_path):
    """爬取单个关键词的最新笔记"""
    logger.info(f"开始爬取关键词: {keyword}")

    try:
        success, msg, notes = xhs_apis.search_some_note(
            query=keyword,
            require_num=config['search_num'],
            cookies_str=cookies_str,
            sort_type_choice=config['sort_type'],
            note_type=config['note_type'],
            note_time=config['note_time'],
            note_range=0,
            pos_distance=0,
            geo=None,
            proxies=None
        )
    except Exception as e:
        logger.error(f"爬取关键词 {keyword} 异常: {e}")
        return 0

    if not success:
        logger.warning(f"爬取关键词 {keyword} 失败: {msg}")
        if 'NoneType' in str(msg):
            logger.error("⚠️ Cookie可能已失效！请删除 .saved_cookies.json 后重启容器重新登录")
        return 0

    # 过滤掉非笔记类型
    notes = [n for n in notes if n.get('model_type') == 'note']
    logger.info(f"关键词 [{keyword}] 获取到 {len(notes)} 条笔记")

    new_count = 0
    note_list = []

    for note in notes:
        note_id = note.get('id', '')
        if note_id in crawled_ids:
            logger.debug(f"跳过已爬取笔记: {note_id}")
            continue

        xsec_token = note.get('xsec_token', '')
        note_url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={xsec_token}"

        try:
            s, m, note_info = xhs_apis.get_note_info(note_url, cookies_str)
            if s and note_info:
                note_info = note_info['data']['items'][0]
                note_info['url'] = note_url
                note_info['search_keyword'] = keyword
                note_info = handle_note_info(note_info)
                note_list.append(note_info)
                crawled_ids.add(note_id)
                new_count += 1
                logger.info(f"新笔记 [{note_id}]: {note_info.get('title', '无标题')[:30]}")
        except Exception as e:
            logger.warning(f"获取笔记详情失败 {note_id}: {e}")
            continue

    # 保存结果
    if note_list:
        if config['save_choice'] in ('all', 'media', 'media-video', 'media-image'):
            download_note(note_list, base_path['media'], config['save_choice'])
        if config['save_choice'] in ('all', 'excel'):
            from datetime import datetime
            ts = datetime.now().strftime('%Y%m%d_%H%M')
            excel_name = f"{keyword}_{ts}"
            file_path = os.path.join(base_path['excel'], f'{excel_name}.xlsx')
            save_to_xlsx(note_list, file_path)

        # 保存去重记录
        save_crawled_ids(record_path, crawled_ids)
        logger.info(f"关键词 [{keyword}] 本轮新增 {new_count} 条笔记，已保存")
    else:
        logger.info(f"关键词 [{keyword}] 本轮无新笔记")

    return new_count


def run():
    """主循环：轮询爬取多个关键词"""
    config = load_config()
    logger.info("=" * 50)
    logger.info("小红书定时爬虫启动")
    logger.info(f"登录方式: {'扫码登录' if config['login_mode'] == 'qrcode' else '手动Cookie'}")
    logger.info(f"关键词: {config['keywords']}")
    logger.info(f"每个关键词爬取数量: {config['search_num']}")
    logger.info(f"关键词间隔: {config['interval_minutes']} 分钟")
    logger.info(f"轮次间隔: {config['round_interval_hours']} 小时")
    logger.info(f"保存方式: {config['save_choice']}")
    logger.info(f"排序方式: {config['sort_type']} (1=最新)")
    logger.info(f"笔记类型: {config['note_type']}")
    logger.info(f"时间范围: {config['note_time']} (1=一天内)")
    logger.info("=" * 50)

    # 获取cookie
    cookies_str = get_cookies(config)
    if not cookies_str:
        logger.error("获取Cookie失败，程序退出")
        logger.error("解决办法：")
        logger.error("  1. 扫码模式：浏览器打开 http://群晖IP:5000 扫码登录")
        logger.error("  2. Cookie模式：设置 LOGIN_MODE=cookie 和 COOKIES='你的cookie'")
        sys.exit(1)

    # 数据目录
    media_base_path = os.path.join(DATAS_DIR, 'media_datas')
    excel_base_path = os.path.join(DATAS_DIR, 'excel_datas')
    for p in [media_base_path, excel_base_path]:
        os.makedirs(p, exist_ok=True)
    base_path = {'media': media_base_path, 'excel': excel_base_path}

    xhs_apis = XHS_Apis()

    # 去重记录文件
    record_path = os.path.join(base_path['excel'], '.crawled_records.json')
    crawled_ids = load_crawled_ids(record_path)
    logger.info(f"已加载去重记录: {len(crawled_ids)} 条")

    round_num = 0
    while True:
        round_num += 1
        logger.info(f"\n{'='*50}\n第 {round_num} 轮爬取开始\n{'='*50}")

        for i, keyword in enumerate(config['keywords']):
            try:
                new_count = crawl_keyword(
                    xhs_apis, keyword, config, cookies_str,
                    base_path, crawled_ids, record_path
                )
                # 第一个关键词就失败，可能cookie失效
                if new_count == 0 and i == 0:
                    if not validate_cookies(cookies_str):
                        logger.error("Cookie已失效，尝试重新登录...")
                        new_cookies = get_cookies(config)
                        if new_cookies:
                            cookies_str = new_cookies
                            logger.info("重新登录成功，继续爬取")
                        else:
                            logger.error("重新登录失败，程序退出")
                            sys.exit(1)
            except Exception as e:
                logger.error(f"爬取关键词 {keyword} 异常: {e}")

            # 关键词之间的间隔
            if i < len(config['keywords']) - 1:
                base_interval = config['interval_minutes'] * 60
                jitter = random.uniform(0.8, 1.2)
                wait_seconds = int(base_interval * jitter)
                logger.info(f"等待 {wait_seconds // 60} 分 {wait_seconds % 60} 秒后爬取下一个关键词...")
                time.sleep(wait_seconds)

        # 一轮结束
        round_wait = config['round_interval_hours'] * 3600
        jitter = random.uniform(0.9, 1.1)
        round_wait = int(round_wait * jitter)
        hours = round_wait // 3600
        minutes = (round_wait % 3600) // 60
        logger.info(f"\n第 {round_num} 轮爬取完成，{hours} 小时 {minutes} 分钟后开始下一轮...")
        time.sleep(round_wait)


if __name__ == '__main__':
    run()
