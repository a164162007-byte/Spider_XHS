"""
小红书爬虫 Web 后台 - Docker 版
提供浏览器可操作的完整 Web 界面，覆盖原项目所有功能
原有代码 (spider/, apis/, xhs_utils/) 一个字不动

运行流程：
  1. 容器启动 → Web后台自动运行（无需任何环境变量）
  2. 浏览器打开 http://群晖IP:5000
  3. 先扫码登录 → Cookie自动保存
  4. 然后使用搜索/爬取/定时等功能
  5. 重启容器后Cookie自动复用，无需重复登录

Docker 使用：
  docker run -d -p 5000:5000 -v /你的路径/datas:/app/datas ghcr.io/a164162007-byte/spider_xhs:latest
"""
import os
import sys
import json
import time
import base64
import threading
import traceback
from io import BytesIO
from datetime import datetime
from pathlib import Path

import qrcode
from loguru import logger
from flask import Flask, Response, jsonify, request

# 原项目代码（一个字不动）
from apis.xhs_pc_apis import XHS_Apis
from apis.xhs_pc_login_apis import XHSLoginApi
from spider.spider import Data_Spider
from xhs_utils.data_util import handle_note_info, download_note, save_to_xlsx

# ========== 路径配置 ==========
DATAS_DIR = '/app/datas'
COOKIE_FILE = os.path.join(DATAS_DIR, '.saved_cookies.json')
QRCODE_IMAGE = os.path.join(DATAS_DIR, 'qrcode_login.png')
MEDIA_DIR = os.path.join(DATAS_DIR, 'media_datas')
EXCEL_DIR = os.path.join(DATAS_DIR, 'excel_datas')

# ========== 全局状态 ==========
app_state = {
    # 登录
    'login_status': 'not_logged',   # not_logged / scanning / success / failed / expired
    'cookies_str': None,
    'user_info': None,
    'qr_image_b64': None,
    'login_message': '未登录',
    'cookie_valid': False,          # Cookie是否经过验证有效

    # 任务
    'current_task': None,           # 当前运行的任务描述
    'task_running': False,
    'task_logs': [],                 # 最近的任务日志（最多200条）
    'task_progress': '',             # 任务进度描述

    # 定时爬取
    'scheduled_running': False,
    'scheduled_config': None,
    'scheduled_round': 0,
}
state_lock = threading.Lock()

# 日志收集器 - 把 loguru 的日志同时输出到 Web
class WebLogHandler:
    def __init__(self):
        self.logs = []
        self.max_logs = 300

    def write(self, message):
        record = message.record
        level = record['level'].name
        msg = record['message']
        ts = record['time'].strftime('%H:%M:%S')
        line = f"[{ts}] [{level}] {msg}"
        with state_lock:
            self.logs.append(line)
            if len(self.logs) > self.max_logs:
                self.logs = self.logs[-self.max_logs:]
            app_state['task_logs'] = list(self.logs)

web_log = WebLogHandler()
logger.add(web_log.write, level="INFO", format="{message}")


# ========== Cookie 管理 ==========
def load_saved_cookies():
    """从文件加载已保存的cookie，返回 (cookies_str, login_time) 或 (None, None)"""
    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                cookies_str = data.get('cookies', '')
                if cookies_str:
                    return cookies_str, data.get('login_time', '')
        except Exception:
            pass
    return None, None


def save_cookies_to_file(cookies_str):
    """保存cookie到文件"""
    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    data = {
        'cookies': cookies_str,
        'login_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open(COOKIE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def validate_cookies(cookies_str):
    """验证cookie是否有效（发一次真实API请求）"""
    try:
        xhs_apis = XHS_Apis()
        success, msg, _ = xhs_apis.search_some_note(
            query="test", require_num=1, cookies_str=cookies_str,
            sort_type_choice=1, note_type=0, note_time=0,
            note_range=0, pos_distance=0, geo=None, proxies=None
        )
        return success
    except Exception:
        return False


# ========== 扫码登录 ==========
def do_qrcode_login():
    """执行扫码登录流程，结果写入 app_state"""
    login_api = XHSLoginApi()

    with state_lock:
        app_state['login_status'] = 'scanning'
        app_state['login_message'] = '正在生成二维码...'

    # 1. 生成初始 cookies
    logger.info('[1/4] 生成初始cookies...')
    cookies = login_api.generate_init_cookies()

    # 2. 获取二维码
    logger.info('[2/4] 获取二维码...')
    success, msg, qr_data = login_api.generate_qrcode(cookies)
    if not success:
        logger.error(f'获取二维码失败: {msg}')
        with state_lock:
            app_state['login_status'] = 'failed'
            app_state['login_message'] = f'获取二维码失败: {msg}'
        return

    cookies = qr_data['cookies']

    # 生成二维码图片
    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(qr_data['qr_url'])
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    os.makedirs(DATAS_DIR, exist_ok=True)
    img.save(QRCODE_IMAGE)

    buf = BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

    with state_lock:
        app_state['qr_image_b64'] = qr_b64
        app_state['login_message'] = '请使用小红书APP扫描二维码'

    logger.info('二维码已生成，请用小红书APP扫码')

    # 3. 等待扫码
    logger.info('[3/4] 等待扫码...')
    while True:
        success, msg, cookies = login_api.check_qrcode_status(
            qr_data['qr_id'], qr_data['code'], cookies
        )
        if success:
            break
        if msg == '二维码已过期':
            logger.error('二维码已过期')
            with state_lock:
                app_state['login_status'] = 'expired'
                app_state['login_message'] = '二维码已过期，请点击重新获取'
                app_state['qr_image_b64'] = None
            return
        time.sleep(2)

    # 4. 验证
    logger.info('[4/4] 验证登录状态...')
    success, user_info, cookies = login_api.get_user_info(cookies)
    nickname = user_info.get('nickname', '未知') if success else '未知'
    red_id = user_info.get('red_id', '未知') if success else '未知'
    cookies_str = login_api.cookies_to_str(cookies)

    save_cookies_to_file(cookies_str)

    with state_lock:
        app_state['login_status'] = 'success'
        app_state['cookies_str'] = cookies_str
        app_state['cookie_valid'] = True
        app_state['user_info'] = {'nickname': nickname, 'red_id': red_id}
        app_state['login_message'] = f'已登录: {nickname}'
        app_state['qr_image_b64'] = None

    logger.success(f'扫码登录成功！用户: {nickname}')


# ========== 任务执行 ==========
def get_cookies_str():
    """获取当前有效的 cookie，优先内存→文件"""
    with state_lock:
        if app_state['cookies_str']:
            return app_state['cookies_str']
    saved, _ = load_saved_cookies()
    if saved:
        with state_lock:
            app_state['cookies_str'] = saved
        return saved
    return None


def get_base_path():
    """获取数据保存路径"""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    os.makedirs(EXCEL_DIR, exist_ok=True)
    return {'media': MEDIA_DIR, 'excel': EXCEL_DIR}


def task_crawl_notes(note_urls):
    """任务：爬取笔记列表"""
    cookies_str = get_cookies_str()
    if not cookies_str:
        logger.error("未登录，请先扫码登录")
        return

    base_path = get_base_path()
    spider = Data_Spider()

    with state_lock:
        app_state['task_running'] = True
        app_state['current_task'] = f'爬取笔记 ({len(note_urls)}条)'

    try:
        spider.spider_some_note(note_urls, cookies_str, base_path, 'all', 'notes')
        logger.success(f'笔记爬取完成')
    except Exception as e:
        logger.error(f'爬取笔记失败: {e}')
    finally:
        with state_lock:
            app_state['task_running'] = False
            app_state['current_task'] = None


def task_crawl_user(user_url, save_choice='all'):
    """任务：爬取用户所有笔记"""
    cookies_str = get_cookies_str()
    if not cookies_str:
        logger.error("未登录，请先扫码登录")
        return

    base_path = get_base_path()
    spider = Data_Spider()

    with state_lock:
        app_state['task_running'] = True
        app_state['current_task'] = f'爬取用户笔记'

    try:
        spider.spider_user_all_note(user_url, cookies_str, base_path, save_choice)
        logger.success(f'用户笔记爬取完成')
    except Exception as e:
        logger.error(f'爬取用户笔记失败: {e}')
    finally:
        with state_lock:
            app_state['task_running'] = False
            app_state['current_task'] = None


def task_search_notes(query, require_num, save_choice, sort_type, note_type, note_time):
    """任务：搜索关键词笔记"""
    cookies_str = get_cookies_str()
    if not cookies_str:
        logger.error("未登录，请先扫码登录")
        return

    base_path = get_base_path()
    spider = Data_Spider()

    with state_lock:
        app_state['task_running'] = True
        app_state['current_task'] = f'搜索: {query}'

    try:
        spider.spider_some_search_note(
            query, require_num, cookies_str, base_path, save_choice,
            sort_type_choice=sort_type, note_type=note_type, note_time=note_time
        )
        logger.success(f'搜索完成: {query}')
    except Exception as e:
        logger.error(f'搜索失败: {e}')
    finally:
        with state_lock:
            app_state['task_running'] = False
            app_state['current_task'] = None


def task_scheduled_crawl(config):
    """任务：定时轮询爬取多关键词"""
    cookies_str = get_cookies_str()
    if not cookies_str:
        logger.error("未登录，请先扫码登录")
        return

    keywords = config['keywords']
    search_num = config.get('search_num', 20)
    interval_min = config.get('interval_minutes', 30)
    round_hours = config.get('round_interval_hours', 4)
    save_choice = config.get('save_choice', 'all')
    sort_type = config.get('sort_type', 1)
    note_type_val = config.get('note_type', 0)
    note_time_val = config.get('note_time', 1)

    base_path = get_base_path()
    spider = Data_Spider()

    # 去重
    record_path = os.path.join(EXCEL_DIR, '.crawled_records.json')
    crawled_ids = set()
    if os.path.exists(record_path):
        try:
            with open(record_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                crawled_ids = set(data.get('crawled_ids', []))
        except Exception:
            pass

    with state_lock:
        app_state['scheduled_running'] = True
        app_state['scheduled_config'] = config

    round_num = 0
    import random

    while True:
        with state_lock:
            if not app_state['scheduled_running']:
                logger.info("定时爬取已停止")
                break

        round_num += 1
        with state_lock:
            app_state['scheduled_round'] = round_num

        logger.info(f"第 {round_num} 轮爬取开始，关键词: {keywords}")

        for i, keyword in enumerate(keywords):
            with state_lock:
                if not app_state['scheduled_running']:
                    break

            with state_lock:
                app_state['current_task'] = f'第{round_num}轮 - 搜索: {keyword}'
                app_state['task_running'] = True

            try:
                success, msg, notes = spider.xhs_apis.search_some_note(
                    query=keyword, require_num=search_num, cookies_str=cookies_str,
                    sort_type_choice=sort_type, note_type=note_type_val,
                    note_time=note_time_val, note_range=0, pos_distance=0,
                    geo=None, proxies=None
                )
                if not success:
                    logger.warning(f'搜索 {keyword} 失败: {msg}')
                    if 'NoneType' in str(msg):
                        logger.error('Cookie可能已失效，停止定时任务')
                        with state_lock:
                            app_state['scheduled_running'] = False
                            app_state['cookie_valid'] = False
                        break
                    continue

                notes = [n for n in notes if n.get('model_type') == 'note']
                logger.info(f'关键词 [{keyword}] 获取 {len(notes)} 条笔记')

                note_urls = []
                for note in notes:
                    note_id = note.get('id', '')
                    if note_id in crawled_ids:
                        continue
                    xsec_token = note.get('xsec_token', '')
                    note_url = f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={xsec_token}"
                    note_urls.append(note_url)
                    crawled_ids.add(note_id)

                if note_urls:
                    spider.spider_some_note(note_urls, cookies_str, base_path, save_choice, keyword)
                    # 保存去重记录
                    os.makedirs(os.path.dirname(record_path), exist_ok=True)
                    with open(record_path, 'w', encoding='utf-8') as f:
                        json.dump({'crawled_ids': list(crawled_ids)}, f, ensure_ascii=False)
                    logger.info(f'关键词 [{keyword}] 新增 {len(note_urls)} 条')
                else:
                    logger.info(f'关键词 [{keyword}] 无新笔记')

            except Exception as e:
                logger.error(f'搜索 {keyword} 异常: {e}')

            # 关键词间隔 + 随机抖动
            if i < len(keywords) - 1:
                import random as _r
                wait = int(interval_min * 60 * _r.uniform(0.8, 1.2))
                logger.info(f'等待 {wait // 60}分{wait % 60}秒 后爬取下一个关键词...')
                time.sleep(wait)

        with state_lock:
            app_state['task_running'] = False
            app_state['current_task'] = None

        # 轮次间隔
        import random as _r
        wait = int(round_hours * 3600 * _r.uniform(0.9, 1.1))
        h, m = wait // 3600, (wait % 3600) // 60
        logger.info(f'第 {round_num} 轮完成，{h}小时{m}分钟后开始下一轮')

        # 分段 sleep，方便随时停止
        for _ in range(wait):
            with state_lock:
                if not app_state['scheduled_running']:
                    break
            time.sleep(1)

    with state_lock:
        app_state['scheduled_running'] = False
        app_state['scheduled_config'] = None
        app_state['task_running'] = False
        app_state['current_task'] = None


# ========== Flask Web App ==========
app = Flask(__name__)


@app.route('/')
def index():
    html = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小红书爬虫</title>
<style>
:root{--bg:#0D1117;--card:#161B22;--border:#30363D;--text:#c9d1d9;--muted:#8b949e;--red:#FF2D2D;--blue:#58A6FF;--green:#3FB950;--yellow:#D29922}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.nav{background:var(--card);border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.nav h1{color:var(--red);font-size:18px}
.nav-right{display:flex;align-items:center;gap:12px}
.login-badge{padding:4px 12px;border-radius:12px;font-size:12px;font-weight:600;cursor:pointer;transition:all .2s}
.badge-logged{background:#0d2818;color:var(--green)}
.badge-not{background:#2d0f0f;color:var(--red)}
.badge-logging{background:#0d2137;color:var(--blue)}
.tabs{display:flex;background:var(--card);border-bottom:1px solid var(--border);padding:0 16px;overflow-x:auto}
.tab{padding:12px 20px;cursor:pointer;color:var(--muted);font-size:14px;border-bottom:2px solid transparent;white-space:nowrap;transition:all .2s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--blue);border-bottom-color:var(--blue)}
.main{max-width:900px;margin:20px auto;padding:0 16px}
.page{display:none}
.page.active{display:block}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:16px}
.card h2{font-size:16px;margin-bottom:12px;color:var(--text)}
.form-group{margin-bottom:14px}
.form-group label{display:block;font-size:13px;color:var(--muted);margin-bottom:4px}
.form-group input,.form-group textarea,.form-group select{width:100%;padding:8px 12px;background:#0d1117;border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:14px;outline:none}
.form-group textarea{min-height:80px;resize:vertical;font-family:monospace}
.form-group input:focus,.form-group textarea:focus{border-color:var(--blue)}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row .form-group{flex:1;min-width:120px}
.btn{padding:8px 20px;border-radius:6px;border:none;cursor:pointer;font-size:14px;font-weight:500;transition:all .2s}
.btn-primary{background:var(--red);color:#fff}
.btn-primary:hover{background:#E02020}
.btn-primary:disabled{background:#666;cursor:not-allowed}
.btn-secondary{background:var(--border);color:var(--text)}
.btn-secondary:hover{background:#444}
.btn-danger{background:#8B0000;color:#fff}
.btn-danger:hover{background:#A00}
.btn-sm{padding:5px 12px;font-size:12px}
.qr-box{display:flex;justify-content:center;padding:20px}
.qr-box img{border-radius:8px;max-width:260px}
.log-box{background:#0d1117;border:1px solid var(--border);border-radius:6px;padding:12px;max-height:400px;overflow-y:auto;font-family:monospace;font-size:12px;line-height:1.6}
.log-box .log-info{color:var(--text)}
.log-box .log-success{color:var(--green)}
.log-box .log-warning{color:var(--yellow)}
.log-box .log-error{color:var(--red)}
.status-bar{display:flex;align-items:center;gap:8px;padding:10px 14px;background:var(--card);border:1px solid var(--border);border-radius:8px;margin-bottom:14px;font-size:13px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot-green{background:var(--green)}
.dot-red{background:var(--red)}
.dot-yellow{background:var(--yellow)}
.dot-blue{background:var(--blue)}
.chip{display:inline-block;padding:3px 10px;border-radius:12px;font-size:12px;margin:2px}
.chip-blue{background:#0d2137;color:var(--blue)}
.chip-green{background:#0d2818;color:var(--green)}
.chip-red{background:#2d0f0f;color:var(--red)}
.task-info{font-size:13px;color:var(--muted);margin-bottom:10px}
.schedule-cfg{font-size:12px;color:var(--muted);line-height:1.8}
.login-required{background:#2d0f0f;border:1px solid #5c1a1a;border-radius:10px;padding:30px;text-align:center;margin-bottom:16px}
.login-required p{color:var(--red);font-size:15px;margin-bottom:12px}
.login-required .btn{margin-top:8px}
@media(max-width:600px){.row{flex-direction:column}.row .form-group{min-width:auto}.nav h1{font-size:15px}}
</style>
</head>
<body>
<div class="nav">
  <h1>📕 小红书爬虫</h1>
  <div class="nav-right">
    <span id="loginBadge" class="login-badge badge-not" onclick="switchTab('login')">未登录</span>
  </div>
</div>
<div class="tabs">
  <div class="tab active" data-page="login">🔑 登录</div>
  <div class="tab" data-page="search">🔍 搜索笔记</div>
  <div class="tab" data-page="notes">📋 爬取笔记</div>
  <div class="tab" data-page="user">👤 爬取用户</div>
  <div class="tab" data-page="scheduled">⏰ 定时爬取</div>
  <div class="tab" data-page="logs">📄 运行日志</div>
</div>
<div class="main">

<!-- 登录页 -->
<div id="page-login" class="page active">
  <div class="card">
    <h2>扫码登录</h2>
    <div class="status-bar">
      <span id="loginDot" class="dot dot-red"></span>
      <span id="loginStatus">未登录</span>
    </div>
    <div id="qrArea" class="qr-box">
      <p style="color:var(--muted)">点击下方按钮获取二维码</p>
    </div>
    <div style="text-align:center;margin-top:12px">
      <button id="btnQR" class="btn btn-primary" onclick="startLogin()">获取二维码</button>
      <button class="btn btn-secondary" onclick="checkCookie()" style="margin-left:8px">验证Cookie</button>
      <button class="btn btn-danger btn-sm" onclick="clearCookie()" style="margin-left:8px">清除Cookie</button>
    </div>
    <div style="margin-top:16px;font-size:12px;color:var(--muted);line-height:1.8">
      <b>使用流程：</b><br>
      1. 点击「获取二维码」<br>
      2. 打开小红书APP → 扫一扫 → 确认登录<br>
      3. 登录成功后Cookie自动保存，下次无需再扫<br>
      4. 也可以在群晖 File Station 查看 <code>datas/qrcode_login.png</code> 扫码<br><br>
      <b>⚠️ 注意：</b>必须先登录才能使用搜索和爬取功能
    </div>
  </div>
</div>

<!-- 搜索笔记页 -->
<div id="page-search" class="page">
  <div id="searchLoginReq" class="login-required" style="display:none">
    <p>⚠️ 请先扫码登录后使用搜索功能</p>
    <button class="btn btn-primary" onclick="switchTab('login')">去登录</button>
  </div>
  <div id="searchForm" class="card">
    <h2>搜索关键词笔记</h2>
    <div class="form-group">
      <label>关键词</label>
      <input id="searchQuery" placeholder="如：榴莲">
    </div>
    <div class="row">
      <div class="form-group"><label>爬取数量</label><input id="searchNum" type="number" value="20"></div>
      <div class="form-group"><label>保存方式</label>
        <select id="searchSave"><option value="all">全部(媒体+Excel)</option><option value="media">媒体文件</option><option value="excel">仅Excel</option><option value="media-image">仅图片</option><option value="media-video">仅视频</option></select>
      </div>
    </div>
    <div class="row">
      <div class="form-group"><label>排序方式</label>
        <select id="searchSort"><option value="0">综合排序</option><option value="1" selected>最新</option><option value="2">最多点赞</option><option value="3">最多评论</option><option value="4">最多收藏</option></select>
      </div>
      <div class="form-group"><label>笔记类型</label>
        <select id="searchNoteType"><option value="0">不限</option><option value="1">视频</option><option value="2">图文</option></select>
      </div>
      <div class="form-group"><label>时间范围</label>
        <select id="searchNoteTime"><option value="0">不限</option><option value="1" selected>一天内</option><option value="2">一周内</option><option value="3">半年内</option></select>
      </div>
    </div>
    <button id="btnSearch" class="btn btn-primary" onclick="doSearch()">开始搜索</button>
  </div>
</div>

<!-- 爬取笔记页 -->
<div id="page-notes" class="page">
  <div id="notesLoginReq" class="login-required" style="display:none">
    <p>⚠️ 请先扫码登录后使用爬取功能</p>
    <button class="btn btn-primary" onclick="switchTab('login')">去登录</button>
  </div>
  <div id="notesForm" class="card">
    <h2>爬取笔记详情</h2>
    <div class="form-group">
      <label>笔记URL（每行一个）</label>
      <textarea id="noteUrls" placeholder="https://www.xiaohongshu.com/explore/xxxx?xsec_token=xxx&#10;https://www.xiaohongshu.com/explore/yyyy?xsec_token=yyy"></textarea>
    </div>
    <button id="btnNotes" class="btn btn-primary" onclick="doCrawlNotes()">开始爬取</button>
  </div>
</div>

<!-- 爬取用户页 -->
<div id="page-user" class="page">
  <div id="userLoginReq" class="login-required" style="display:none">
    <p>⚠️ 请先扫码登录后使用爬取功能</p>
    <button class="btn btn-primary" onclick="switchTab('login')">去登录</button>
  </div>
  <div id="userForm" class="card">
    <h2>爬取用户所有笔记</h2>
    <div class="form-group">
      <label>用户主页URL</label>
      <input id="userUrl" placeholder="https://www.xiaohongshu.com/user/profile/xxxx?xsec_token=xxx">
    </div>
    <div class="form-group"><label>保存方式</label>
      <select id="userSave"><option value="all">全部(媒体+Excel)</option><option value="media">媒体文件</option><option value="excel">仅Excel</option><option value="media-image">仅图片</option><option value="media-video">仅视频</option></select>
    </div>
    <button id="btnUser" class="btn btn-primary" onclick="doCrawlUser()">开始爬取</button>
  </div>
</div>

<!-- 定时爬取页 -->
<div id="page-scheduled" class="page">
  <div id="schedLoginReq" class="login-required" style="display:none">
    <p>⚠️ 请先扫码登录后使用定时爬取功能</p>
    <button class="btn btn-primary" onclick="switchTab('login')">去登录</button>
  </div>
  <div id="schedForm" class="card">
    <h2>定时爬取（多关键词轮询）</h2>
    <div class="status-bar">
      <span id="schedDot" class="dot dot-red"></span>
      <span id="schedStatus">未运行</span>
    </div>
    <div class="form-group">
      <label>关键词（逗号分隔）</label>
      <input id="schedKeywords" placeholder="榴莲,芒果,椰子">
    </div>
    <div class="row">
      <div class="form-group"><label>每个关键词爬取数量</label><input id="schedNum" type="number" value="20"></div>
      <div class="form-group"><label>关键词间隔（分钟）</label><input id="schedInterval" type="number" value="30"></div>
      <div class="form-group"><label>轮次间隔（小时）</label><input id="schedRound" type="number" value="4" step="0.5"></div>
    </div>
    <div class="row">
      <div class="form-group"><label>保存方式</label>
        <select id="schedSave"><option value="all">全部</option><option value="media">媒体</option><option value="excel">Excel</option></select>
      </div>
      <div class="form-group"><label>排序</label>
        <select id="schedSort"><option value="1" selected>最新</option><option value="0">综合</option><option value="2">最多点赞</option></select>
      </div>
      <div class="form-group"><label>时间范围</label>
        <select id="schedTime"><option value="1" selected>一天内</option><option value="0">不限</option><option value="2">一周内</option></select>
      </div>
    </div>
    <button id="btnSchedStart" class="btn btn-primary" onclick="startSched()">启动定时爬取</button>
    <button id="btnSchedStop" class="btn btn-danger" onclick="stopSched()" style="display:none">停止定时爬取</button>
  </div>
  <div id="schedInfo" class="card" style="display:none">
    <h2>运行状态</h2>
    <div id="schedDetail" class="schedule-cfg"></div>
  </div>
</div>

<!-- 日志页 -->
<div id="page-logs" class="page">
  <div class="card">
    <h2>运行日志</h2>
    <div class="status-bar" id="taskStatusBar" style="display:none">
      <span class="dot dot-blue"></span>
      <span id="taskInfo">-</span>
    </div>
    <div style="margin-bottom:10px">
      <button class="btn btn-secondary btn-sm" onclick="clearLogs()">清空日志</button>
      <button class="btn btn-secondary btn-sm" onclick="refreshLogs()" style="margin-left:6px">刷新</button>
      <label style="margin-left:12px;font-size:12px;color:var(--muted)"><input type="checkbox" id="autoScroll" checked> 自动滚动</label>
    </div>
    <div id="logBox" class="log-box"></div>
  </div>
</div>

</div>

<script>
// ====== 全局登录状态 ======
let isLoggedIn = false;

function updateLoginUI(loggedIn, nickname) {
  isLoggedIn = loggedIn;
  const badge = document.getElementById('loginBadge');
  // 顶部徽章
  if (loggedIn) {
    badge.className = 'login-badge badge-logged';
    badge.textContent = nickname || '已登录';
  } else {
    badge.className = 'login-badge badge-not';
    badge.textContent = '未登录';
  }
  // 功能页登录提示
  const reqs = ['searchLoginReq','notesLoginReq','userLoginReq','schedLoginReq'];
  const forms = ['searchForm','notesForm','userForm','schedForm'];
  reqs.forEach((id, i) => {
    document.getElementById(id).style.display = loggedIn ? 'none' : 'block';
    document.getElementById(forms[i]).style.display = loggedIn ? 'block' : 'none';
  });
}

// Tab 切换
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => {
    t.classList.remove('active');
    if (t.dataset.page === name) t.classList.add('active');
  });
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
}
document.querySelectorAll('.tab').forEach(t => {
  t.onclick = () => switchTab(t.dataset.page);
});

// 通用请求
async function api(path, opts = {}) {
  try {
    let r = await fetch(path, opts);
    return await r.json();
  } catch (e) {
    console.error(e);
    return {ok: false, msg: '网络请求失败，请检查容器是否在运行'};
  }
}

// ====== 登录 ======
async function startLogin() {
  document.getElementById('btnQR').disabled = true;
  document.getElementById('qrArea').innerHTML = '<p style="color:var(--muted)">正在生成二维码...</p>';
  await api('/api/login/qrcode');
  pollLogin();
}
function pollLogin() {
  let t = setInterval(async () => {
    let d = await api('/api/login/status');
    let badge = document.getElementById('loginBadge');
    let dot = document.getElementById('loginDot');
    let st = document.getElementById('loginStatus');
    if (d.status === 'scanning' && d.qr_b64) {
      document.getElementById('qrArea').innerHTML = '<img src="data:image/png;base64,' + d.qr_b64 + '">';
      st.textContent = '等待扫码...'; dot.className = 'dot dot-yellow';
      badge.className = 'login-badge badge-logging'; badge.textContent = '扫码中';
    } else if (d.status === 'success') {
      document.getElementById('qrArea').innerHTML = '<div style="font-size:48px">✅</div><p style="color:var(--green);margin-top:8px">' + d.message + '</p>';
      st.textContent = d.message; dot.className = 'dot dot-green';
      let nick = d.user_info ? d.user_info.nickname : '已登录';
      updateLoginUI(true, nick);
      document.getElementById('btnQR').disabled = false;
      clearInterval(t);
    } else if (d.status === 'expired') {
      document.getElementById('qrArea').innerHTML = '<p style="color:var(--yellow)">二维码已过期</p><button class="btn btn-primary btn-sm" onclick="startLogin()" style="margin-top:8px">重新获取</button>';
      st.textContent = '二维码过期'; dot.className = 'dot dot-red';
      updateLoginUI(false);
      document.getElementById('btnQR').disabled = false;
      clearInterval(t);
    } else if (d.status === 'failed') {
      st.textContent = d.message; dot.className = 'dot dot-red';
      updateLoginUI(false);
      document.getElementById('btnQR').disabled = false;
      clearInterval(t);
    }
  }, 2000);
}
async function checkCookie() {
  let d = await api('/api/login/check');
  let dot = document.getElementById('loginDot');
  let st = document.getElementById('loginStatus');
  if (d.valid) {
    updateLoginUI(true, d.nickname || '已登录');
    dot.className = 'dot dot-green';
    st.textContent = 'Cookie有效: ' + (d.nickname || '');
  } else {
    updateLoginUI(false);
    dot.className = 'dot dot-red';
    st.textContent = 'Cookie无效或已过期，请重新扫码登录';
  }
}
async function clearCookie() {
  if (!confirm('确认清除已保存的Cookie？清除后需重新扫码登录。')) return;
  await api('/api/login/clear');
  updateLoginUI(false);
  document.getElementById('loginDot').className = 'dot dot-red';
  document.getElementById('loginStatus').textContent = 'Cookie已清除，请重新扫码登录';
  document.getElementById('qrArea').innerHTML = '<p style="color:var(--muted)">Cookie已清除</p>';
}

// ====== 搜索 ======
async function doSearch() {
  let q = document.getElementById('searchQuery').value.trim();
  if (!q) { alert('请输入关键词'); return; }
  if (!isLoggedIn) { alert('请先扫码登录'); switchTab('login'); return; }
  document.getElementById('btnSearch').disabled = true;
  let d = await api('/api/task/search', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    query: q, num: parseInt(document.getElementById('searchNum').value) || 20,
    save: document.getElementById('searchSave').value, sort: parseInt(document.getElementById('searchSort').value),
    note_type: parseInt(document.getElementById('searchNoteType').value), note_time: parseInt(document.getElementById('searchNoteTime').value)
  })});
  document.getElementById('btnSearch').disabled = false;
  if (d.ok) { switchTab('logs'); }
  else { alert(d.msg || '操作失败'); }
}

// ====== 爬取笔记 ======
async function doCrawlNotes() {
  let urls = document.getElementById('noteUrls').value.trim().split('\\n').map(s => s.trim()).filter(s => s);
  if (!urls.length) { alert('请输入笔记URL'); return; }
  if (!isLoggedIn) { alert('请先扫码登录'); switchTab('login'); return; }
  document.getElementById('btnNotes').disabled = true;
  let d = await api('/api/task/notes', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({urls: urls})});
  document.getElementById('btnNotes').disabled = false;
  if (d.ok) { switchTab('logs'); }
  else { alert(d.msg || '操作失败'); }
}

// ====== 爬取用户 ======
async function doCrawlUser() {
  let url = document.getElementById('userUrl').value.trim();
  if (!url) { alert('请输入用户URL'); return; }
  if (!isLoggedIn) { alert('请先扫码登录'); switchTab('login'); return; }
  document.getElementById('btnUser').disabled = true;
  let d = await api('/api/task/user', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    url: url, save: document.getElementById('userSave').value
  })});
  document.getElementById('btnUser').disabled = false;
  if (d.ok) { switchTab('logs'); }
  else { alert(d.msg || '操作失败'); }
}

// ====== 定时爬取 ======
async function startSched() {
  let kw = document.getElementById('schedKeywords').value.trim();
  if (!kw) { alert('请输入关键词'); return; }
  if (!isLoggedIn) { alert('请先扫码登录'); switchTab('login'); return; }
  document.getElementById('btnSchedStart').disabled = true;
  let d = await api('/api/scheduled/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    keywords: kw.split(',').map(s => s.trim()).filter(s => s),
    num: parseInt(document.getElementById('schedNum').value) || 20,
    interval: parseInt(document.getElementById('schedInterval').value) || 30,
    round: parseFloat(document.getElementById('schedRound').value) || 4,
    save: document.getElementById('schedSave').value,
    sort: parseInt(document.getElementById('schedSort').value),
    note_time: parseInt(document.getElementById('schedTime').value)
  })});
  document.getElementById('btnSchedStart').disabled = false;
  if (d.ok) { updateSchedUI(); switchTab('logs'); }
  else { alert(d.msg || '操作失败'); }
}
async function stopSched() {
  await api('/api/scheduled/stop');
  updateSchedUI();
}
async function updateSchedUI() {
  let d = await api('/api/scheduled/status');
  let dot = document.getElementById('schedDot');
  let st = document.getElementById('schedStatus');
  let btnS = document.getElementById('btnSchedStart');
  let btnX = document.getElementById('btnSchedStop');
  let info = document.getElementById('schedInfo');
  if (d.running) {
    dot.className = 'dot dot-green'; st.textContent = '运行中 (第' + d.round + '轮)';
    btnS.style.display = 'none'; btnX.style.display = '';
    info.style.display = '';
    document.getElementById('schedDetail').innerHTML = '关键词: ' + d.config.keywords.map(k => '<span class="chip chip-blue">' + k + '</span>').join('') + '<br>每词爬取: <span class="chip chip-green">' + d.config.num + '条</span> 间隔: <span class="chip chip-green">' + d.config.interval + '分钟</span> 轮次间隔: <span class="chip chip-green">' + d.config.round + '小时</span>';
  } else {
    dot.className = 'dot dot-red'; st.textContent = '未运行';
    btnS.style.display = ''; btnX.style.display = 'none';
    info.style.display = 'none';
  }
}

// ====== 日志 ======
let logTimer = null;
async function refreshLogs() {
  let d = await api('/api/logs');
  let box = document.getElementById('logBox');
  let bar = document.getElementById('taskStatusBar');
  let info = document.getElementById('taskInfo');
  if (d.task_running) { bar.style.display = 'flex'; info.textContent = d.current_task || '运行中'; }
  else { bar.style.display = 'none'; }
  let html = '';
  (d.logs || []).forEach(l => {
    let cls = 'log-info';
    if (l.includes('[SUCCESS]')) cls = 'log-success';
    else if (l.includes('[WARNING]')) cls = 'log-warning';
    else if (l.includes('[ERROR]')) cls = 'log-error';
    html += '<div class="' + cls + '">' + l.replace(/</g, '&lt;') + '</div>';
  });
  box.innerHTML = html;
  if (document.getElementById('autoScroll').checked) { box.scrollTop = box.scrollHeight; }
}
function clearLogs() { fetch('/api/logs/clear'); document.getElementById('logBox').innerHTML = ''; }

// 自动刷新日志
logTimer = setInterval(refreshLogs, 3000);
refreshLogs();

// 页面加载时检查登录状态（不弹提示，静默检查）
(async function() {
  let d = await api('/api/login/check');
  if (d.valid) {
    updateLoginUI(true, d.nickname || '已登录');
    document.getElementById('loginDot').className = 'dot dot-green';
    document.getElementById('loginStatus').textContent = 'Cookie有效: ' + (d.nickname || '');
  } else {
    updateLoginUI(false);
  }
  updateSchedUI();
})();
</script>
</body>
</html>'''
    return Response(html, content_type='text/html; charset=utf-8')


# ========== API 路由 ==========

@app.route('/api/login/status')
def api_login_status():
    with state_lock:
        return jsonify({
            'status': app_state['login_status'],
            'message': app_state['login_message'],
            'qr_b64': app_state.get('qr_image_b64'),
            'user_info': app_state.get('user_info'),
        })


@app.route('/api/login/qrcode')
def api_login_qrcode():
    """触发扫码登录"""
    with state_lock:
        if app_state['login_status'] == 'scanning':
            return jsonify({'ok': True, 'msg': '已在扫码中'})

    t = threading.Thread(target=do_qrcode_login, daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': '正在生成二维码'})


@app.route('/api/login/check')
def api_login_check():
    """检查已保存的cookie是否有效"""
    saved, login_time = load_saved_cookies()
    if saved:
        # 在后台线程验证，避免阻塞请求
        valid = validate_cookies(saved)
        if valid:
            with state_lock:
                app_state['cookies_str'] = saved
                app_state['cookie_valid'] = True
                app_state['login_status'] = 'success'
                app_state['login_message'] = f'已登录 (Cookie保存于 {login_time})'
            return jsonify({'valid': True, 'nickname': f'Cookie保存于 {login_time}'})
        else:
            with state_lock:
                app_state['cookies_str'] = None
                app_state['cookie_valid'] = False
                app_state['login_status'] = 'not_logged'
                app_state['login_message'] = 'Cookie已过期，请重新扫码登录'
            return jsonify({'valid': False})
    return jsonify({'valid': False})


@app.route('/api/login/clear')
def api_login_clear():
    """清除保存的cookie"""
    try:
        if os.path.exists(COOKIE_FILE):
            os.remove(COOKIE_FILE)
    except Exception:
        pass
    with state_lock:
        app_state['cookies_str'] = None
        app_state['cookie_valid'] = False
        app_state['login_status'] = 'not_logged'
        app_state['login_message'] = 'Cookie已清除'
    return jsonify({'ok': True})


# ====== 任务 API ======

@app.route('/api/task/search', methods=['POST'])
def api_task_search():
    data = request.json or {}
    query = data.get('query', '').strip()
    if not query:
        return jsonify({'ok': False, 'msg': '关键词不能为空'})

    cookies_str = get_cookies_str()
    if not cookies_str:
        return jsonify({'ok': False, 'msg': '未登录，请先扫码登录'})

    t = threading.Thread(target=task_search_notes, args=(
        query,
        data.get('num', 20),
        data.get('save', 'all'),
        data.get('sort', 1),
        data.get('note_type', 0),
        data.get('note_time', 1),
    ), daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': f'开始搜索: {query}'})


@app.route('/api/task/notes', methods=['POST'])
def api_task_notes():
    data = request.json or {}
    urls = data.get('urls', [])
    if not urls:
        return jsonify({'ok': False, 'msg': '请输入笔记URL'})

    cookies_str = get_cookies_str()
    if not cookies_str:
        return jsonify({'ok': False, 'msg': '未登录，请先扫码登录'})

    t = threading.Thread(target=task_crawl_notes, args=(urls,), daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': f'开始爬取 {len(urls)} 条笔记'})


@app.route('/api/task/user', methods=['POST'])
def api_task_user():
    data = request.json or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'ok': False, 'msg': '请输入用户URL'})

    cookies_str = get_cookies_str()
    if not cookies_str:
        return jsonify({'ok': False, 'msg': '未登录，请先扫码登录'})

    t = threading.Thread(target=task_crawl_user, args=(
        url,
        data.get('save', 'all'),
    ), daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': '开始爬取用户笔记'})


# ====== 定时爬取 API ======

@app.route('/api/scheduled/start', methods=['POST'])
def api_scheduled_start():
    data = request.json or {}
    keywords = data.get('keywords', [])
    if not keywords:
        return jsonify({'ok': False, 'msg': '关键词不能为空'})

    cookies_str = get_cookies_str()
    if not cookies_str:
        return jsonify({'ok': False, 'msg': '未登录，请先扫码登录'})

    with state_lock:
        if app_state['scheduled_running']:
            return jsonify({'ok': False, 'msg': '定时爬取已在运行中'})

    config = {
        'keywords': keywords,
        'search_num': data.get('num', 20),
        'interval_minutes': data.get('interval', 30),
        'round_interval_hours': data.get('round', 4),
        'save_choice': data.get('save', 'all'),
        'sort_type': data.get('sort', 1),
        'note_time': data.get('note_time', 1),
    }

    t = threading.Thread(target=task_scheduled_crawl, args=(config,), daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': '定时爬取已启动'})


@app.route('/api/scheduled/stop')
def api_scheduled_stop():
    with state_lock:
        app_state['scheduled_running'] = False
    logger.info("正在停止定时爬取...")
    return jsonify({'ok': True, 'msg': '正在停止'})


@app.route('/api/scheduled/status')
def api_scheduled_status():
    with state_lock:
        running = app_state['scheduled_running']
        config = app_state.get('scheduled_config') or {}
        round_num = app_state.get('scheduled_round', 0)
    return jsonify({
        'running': running,
        'round': round_num,
        'config': {
            'keywords': config.get('keywords', []),
            'num': config.get('search_num', 0),
            'interval': config.get('interval_minutes', 0),
            'round': config.get('round_interval_hours', 0),
        } if running else {},
    })


# ====== 日志 API ======

@app.route('/api/logs')
def api_logs():
    with state_lock:
        return jsonify({
            'logs': list(app_state['task_logs']),
            'task_running': app_state['task_running'],
            'current_task': app_state.get('current_task', ''),
        })


@app.route('/api/logs/clear')
def api_logs_clear():
    with state_lock:
        app_state['task_logs'] = []
        web_log.logs = []
    return jsonify({'ok': True})


# ====== 二维码图片 ======

@app.route('/qrcode.png')
def qrcode_image():
    if os.path.exists(QRCODE_IMAGE):
        with open(QRCODE_IMAGE, 'rb') as f:
            return Response(f.read(), content_type='image/png')
    return Response('Not found', status=404)


# ========== 启动 ==========
if __name__ == '__main__':
    # 确保目录存在
    os.makedirs(DATAS_DIR, exist_ok=True)
    os.makedirs(MEDIA_DIR, exist_ok=True)
    os.makedirs(EXCEL_DIR, exist_ok=True)

    # 尝试加载已保存的 cookie
    saved, login_time = load_saved_cookies()
    if saved:
        logger.info(f"发现已保存的Cookie ({login_time})，请在登录页点击「验证Cookie」检查是否有效")
        with state_lock:
            app_state['cookies_str'] = saved
            app_state['login_message'] = f'发现保存的Cookie ({login_time})，请验证'
    else:
        logger.info("未发现保存的Cookie，请扫码登录")

    port = int(os.getenv('WEB_PORT', '5000'))
    logger.info(f"小红书爬虫Web后台启动: http://0.0.0.0:{port}")
    logger.info("操作流程: 浏览器打开 → 扫码登录 → 使用功能")
    logger.info("无需任何环境变量，不需要填写关键词即可启动")

    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
