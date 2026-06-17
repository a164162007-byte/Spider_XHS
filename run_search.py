"""
定时爬取小红书关键词最新笔记
支持多关键词、自定义间隔轮爬、自动去重、扫码登录
"""
import os
import time
import json
import random
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

# Cookie保存路径（持久化到挂载目录）
COOKIE_FILE = '/app/datas/.saved_cookies.json'


def load_config():
    """从环境变量加载配置"""
    keywords_str = os.getenv('SEARCH_KEYWORDS', '')
    if not keywords_str:
        raise ValueError("必须设置环境变量 SEARCH_KEYWORDS，多个关键词用逗号分隔")

    keywords = [k.strip() for k in keywords_str.split(',') if k.strip()]
    if not keywords:
        raise ValueError("SEARCH_KEYWORDS 不能为空")

    config = {
        'login_mode': os.getenv('LOGIN_MODE', 'qrcode'),  # qrcode 或 cookie
        'keywords': keywords,
        'search_num': int(os.getenv('SEARCH_NUM', '20')),
        'interval_minutes': int(os.getenv('INTERVAL_MINUTES', '30')),
        'round_interval_hours': float(os.getenv('ROUND_INTERVAL_HOURS', '4')),
        'save_choice': os.getenv('SAVE_CHOICE', 'all'),
        'sort_type': int(os.getenv('SORT_TYPE', '1')),
        'note_type': int(os.getenv('NOTE_TYPE', '0')),
        'note_time': int(os.getenv('NOTE_TIME', '1')),
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
                    logger.info(f"已从文件加载保存的cookie（上次登录: {data.get('login_time', '未知')}）")
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


def get_cookies(config):
    """获取cookie：优先用保存的 → 扫码登录 → 手动填的"""
    # 1. 先看有没有已保存的cookie
    saved = load_saved_cookies()
    if saved:
        logger.info("使用已保存的cookie，如需重新登录请删除 /app/datas/.saved_cookies.json")
        return saved

    # 2. 扫码登录模式
    if config['login_mode'] == 'qrcode':
        logger.info("未找到保存的cookie，启动扫码登录...")
        login_api = XHSLoginApi()
        cookies_str = login_api.qrcode_login(show_in_terminal=True)
        if cookies_str:
            save_cookies_to_file(cookies_str)
            logger.success("扫码登录成功！Cookie已自动保存")
            return cookies_str
        else:
            logger.error("扫码登录失败")
            return None

    # 3. 手动cookie模式
    cookies_str, _ = init()
    if not cookies_str:
        logger.error("COOKIES 未配置，请设置环境变量 COOKIES 或使用 LOGIN_MODE=qrcode 扫码登录")
        return None

    save_cookies_to_file(cookies_str)
    return cookies_str


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
        logger.error(f"爬取关键词 {keyword} 失败: {e}")
        return

    if not success:
        logger.warning(f"爬取关键词 {keyword} 失败: {msg}")
        return

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
            # 每个关键词单独一个Excel，按时间命名
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

    # 获取cookie（自动扫码 or 手动）
    cookies_str = get_cookies(config)
    if not cookies_str:
        logger.error("获取Cookie失败，退出")
        return

    # 获取base_path
    media_base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'datas/media_datas'))
    excel_base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'datas/excel_datas'))
    for base_path in [media_base_path, excel_base_path]:
        if not os.path.exists(base_path):
            os.makedirs(base_path)
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
            except Exception as e:
                logger.error(f"爬取关键词 {keyword} 异常: {e}")

            # 关键词之间的间隔（最后一个关键词爬完后不需要等）
            if i < len(config['keywords']) - 1:
                # 加随机抖动 ±20%，防止被识别为机器人
                base_interval = config['interval_minutes'] * 60
                jitter = random.uniform(0.8, 1.2)
                wait_seconds = int(base_interval * jitter)
                logger.info(f"等待 {wait_seconds // 60} 分 {wait_seconds % 60} 秒后爬取下一个关键词...")
                time.sleep(wait_seconds)

        # 一轮结束，等待下一轮
        round_wait = config['round_interval_hours'] * 3600
        # 加随机抖动 ±10%
        jitter = random.uniform(0.9, 1.1)
        round_wait = int(round_wait * jitter)
        hours = round_wait // 3600
        minutes = (round_wait % 3600) // 60
        logger.info(f"\n第 {round_num} 轮爬取完成，{hours} 小时 {minutes} 分钟后开始下一轮...")
        time.sleep(round_wait)


if __name__ == '__main__':
    run()
