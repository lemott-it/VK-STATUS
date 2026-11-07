import vk_api
from datetime import datetime
import time
import os
import json
import threading

# ================= НАСТРОЙКИ =================
ACCESS_TOKEN = 'Ваш ключ Api с сайта dev.vk.com'

# Отслеживание онлайна — список ID или screen_name
ONLINE_USERS = ['id1', 'id2', 'id3']

# Глубокий трекинг (лайки + комментарии) — список ID или screen_name
# ВНИМАНИЕ: для каждого пользователя здесь запускается отдельный поток,
# и полный цикл лайков занимает ~15 минут. Не ставьте сюда много людей.
DEEP_TRACK_USERS = ['id1', 'id2', 'id3']

CHECK_INTERVAL = 60          # онлайна (сек)
LIKES_INTERVAL = 3600        # перепроверка лайков (сек)
COMMENTS_INTERVAL = 3600     # перепроверка комментариев (сек)

FRIENDS_COUNT = 30
GROUPS_COUNT = 20
POSTS_PER_WALL = 50

COMMUNITIES_TO_SCAN = []     # ID сообществ для сканирования комментариев (со знаком минус)
COMMENTS_SCAN_DEPTH = 50

LOG_DIR = 'vk_full_logs'
API_VERSION = '5.199'
# ============================================

os.makedirs(LOG_DIR, exist_ok=True)

vk_session = vk_api.VkApi(token=ACCESS_TOKEN)
vk = vk_session.get_api()

_log_lock = threading.Lock()


def log_entry(user_id, category, message):
    """user_id — numeric id (строка). Раздельные логи по пользователю и категории."""
    now = datetime.now()
    line = f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] [{category}] {message}"
    print(line)
    filename = os.path.join(LOG_DIR, f"{user_id}_{category}_{now.strftime('%Y-%m-%d')}.log")
    with _log_lock:
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(line + '\n')


# ---------- БЛОК 1: ОНЛАЙН (МУЛЬТИ) ----------
def online_worker(user_ids):
    """user_ids — список numeric id (строк)."""
    log_entry('system', 'ONLINE', f'Старт отслеживания {len(user_ids)} пользователей: {user_ids}')
    last_states = {}  # {uid: (online, last_seen_ts)}
    ids_str = ','.join(user_ids)

    while True:
        try:
            resp = vk.users.get(
                user_ids=ids_str,
                fields='online,last_seen,screen_name,first_name,last_name'
            )
            for u in resp:
                uid = str(u['id'])
                name = f"{u.get('first_name','')} {u.get('last_name','')}".strip()
                online = u.get('online', 0) == 1
                ls = u.get('last_seen')
                last_seen_ts = ls['time'] if ls else None
                last_seen_dt = datetime.fromtimestamp(last_seen_ts).strftime('%Y-%m-%d %H:%M:%S') if ls else 'недоступно'
                status = 'В СЕТИ' if online else 'НЕ В СЕТИ'

                current = (online, last_seen_ts)
                prev = last_states.get(uid)

                # Пишем, если: первая проверка / сейчас онлайн / состояние изменилось
                if prev is None or online or current != prev:
                    log_entry(uid, 'ONLINE',
                              f"{name} (@{u.get('screen_name')}) — {status}. Последний визит: {last_seen_dt}")

                # Отдельная запись о смене состояния
                if prev is not None and current != prev:
                    if prev[0] != online:
                        log_entry(uid, 'STATE_CHANGE',
                                  f"Смена статуса: {'В СЕТИ' if prev[0] else 'НЕ В СЕТИ'} -> {status}")
                    else:
                        log_entry(uid, 'STATE_CHANGE', f"Обновился last_seen: {last_seen_dt}")

                last_states[uid] = current
        except Exception as e:
            log_entry('system', 'ERROR', f'users.get: {e}')
        time.sleep(CHECK_INTERVAL)

        # ---------- КЭШ ВЛАДЕЛЬЦЕВ ----------
        _owner_cache = {}  # {owner_id: "Название"}

        def get_owner_name(owner_id):
            """Возвращает название сообщества или имя пользователя. Кэширует результат."""
            owner_id = int(owner_id)
            if owner_id in _owner_cache:
                return _owner_cache[owner_id]

            name = f"id{abs(owner_id)}"  # fallback
            try:
                if owner_id < 0:  # сообщество
                    resp = vk.groups.getById(group_id=abs(owner_id), fields='name')
                    if resp:
                        name = resp[0].get('name', name)
                else:  # пользователь
                    resp = vk.users.get(user_ids=owner_id, fields='first_name,last_name')
                    if resp:
                        name = f"{resp[0].get('first_name', '')} {resp[0].get('last_name', '')}".strip() or name
            except Exception as e:
                log_entry('system', 'ERROR', f'get_owner_name {owner_id}: {e}')

            _owner_cache[owner_id] = name
            return name


# ---------- БЛОК 2: ЛАЙКИ ----------
def _get_wall_posts(owner_id, count, uid_for_log):
    try:
        items = vk.wall.get(owner_id=owner_id, count=count, filter='all')['items']
        return [{'type': 'post', 'owner_id': p['owner_id'], 'item_id': p['id']} for p in items]
    except Exception as e:
        log_entry(uid_for_log, 'LIKES_ERROR', f'wall.get {owner_id}: {e}')
        return []


def _get_friends(user_id, count, uid_for_log):
    try:
        return vk.friends.get(user_id=user_id, count=count, order='random')['items']
    except Exception as e:
        log_entry(uid_for_log, 'LIKES_ERROR', f'friends.get: {e}')
        return []


def _get_groups(user_id, count, uid_for_log):
    try:
        return vk.groups.get(user_id=user_id, count=count)['items']
    except Exception as e:
        log_entry(uid_for_log, 'LIKES_ERROR', f'groups.get: {e}')
        return []

def build_likes_targets(user_id):
    uid = str(user_id)
    targets = []

    def _collect(owner_id, count):
        try:
            items = vk.wall.get(owner_id=owner_id, count=count, filter='all')['items']
            for p in items:
                targets.append({
                    'type': 'post',
                    'owner_id': p['owner_id'],
                    'item_id': p['id'],
                    'text_preview': (p.get('text', '') or '')[:80].replace('\n', ' '),
                })
        except Exception as e:
            log_entry(uid, 'LIKES_ERROR', f'wall.get {owner_id}: {e}')

    log_entry(uid, 'LIKES', f'Собираю посты со стены {uid}...')
    _collect(user_id, POSTS_PER_WALL)

    friends = _get_friends(user_id, FRIENDS_COUNT, uid)
    log_entry(uid, 'LIKES', f'Собираю стены {len(friends)} друзей...')
    for fid in friends:
        _collect(fid, POSTS_PER_WALL)
        time.sleep(0.34)

    groups = _get_groups(user_id, GROUPS_COUNT, uid)
    log_entry(uid, 'LIKES', f'Собираю стены {len(groups)} сообществ...')
    for gid in groups:
        _collect(-gid, POSTS_PER_WALL)
        time.sleep(0.34)

    return targets
def likes_worker(user_id):
    uid = str(user_id)
    while True:
        log_entry(uid, 'LIKES', 'Начинаю цикл проверки лайков...')
        targets = build_likes_targets(user_id)
        log_entry(uid, 'LIKES', f'Собрано объектов: {len(targets)}. Начинаю проверку...')

        found = []
        for obj in targets:
            try:
                r = vk.likes.isLiked(
                    type=obj['type'],
                    owner_id=obj['owner_id'],
                    item_id=obj['item_id'],
                    user_id=user_id
                )
                if r.get('liked') == 1:
                    owner_name = get_owner_name(obj['owner_id'])
                    url = f"https://vk.com/wall{obj['owner_id']}_{obj['item_id']}"
                    preview = obj.get('text_preview', '')
                    log_entry(uid, 'LIKE_FOUND',
                              f"[{owner_name}] {url} | \"{preview}\"")
                    found.append({
                        'url': url,
                        'owner_name': owner_name,
                        'owner_id': obj['owner_id'],
                        'item_id': obj['item_id'],
                        'text_preview': preview,
                        'checked_at': datetime.now().isoformat(),
                    })
            except Exception as e:
                log_entry(uid, 'LIKES_ERROR', f"isLiked {obj}: {e}")
            time.sleep(0.34)

        out = os.path.join(LOG_DIR, f"{uid}_likes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(found, f, ensure_ascii=False, indent=2)
        log_entry(uid, 'LIKES', f'Цикл завершён. Найдено {len(found)} лайков. Сохранено: {out}')

        time.sleep(LIKES_INTERVAL)


# ---------- БЛОК 3: КОММЕНТАРИИ ----------
def comments_worker(user_id):
    uid = str(user_id)
    if not COMMUNITIES_TO_SCAN:
        log_entry(uid, 'COMMENTS', 'Список сообществ пуст — блок отключён.')
        return
    while True:
        log_entry(uid, 'COMMENTS', f'Сканирую {len(COMMUNITIES_TO_SCAN)} сообществ...')
        for gid in COMMUNITIES_TO_SCAN:
            try:
                group_name = get_owner_name(gid)
                posts = vk.wall.get(owner_id=gid, count=COMMENTS_SCAN_DEPTH)['items']
                for p in posts:
                    try:
                        cms = vk.wall.getComments(
                            owner_id=p['owner_id'], post_id=p['id'],
                            count=100, sort='desc'
                        )['items']
                        for c in cms:
                            if str(c.get('from_id')) == uid:
                                url = f"https://vk.com/wall{p['owner_id']}_{p['id']}?reply={c['id']}"
                                post_preview = (p.get('text', '') or '')[:60].replace('\n', ' ')
                                log_entry(uid, 'COMMENT_FOUND',
                                          f"[{group_name}] {url} | "
                                          f"{datetime.fromtimestamp(c['date']).strftime('%Y-%m-%d %H:%M:%S')} | "
                                          f"пост: \"{post_preview}\" | "
                                          f"коммент: \"{c.get('text','')[:120]}\"")
                    except Exception as e:
                        log_entry(uid, 'COMMENTS_ERROR', f"getComments {p['owner_id']}_{p['id']}: {e}")
                    time.sleep(0.34)
            except Exception as e:
                log_entry(uid, 'COMMENTS_ERROR', f'wall.get {gid}: {e}')
        time.sleep(COMMENTS_INTERVAL)

# ---------- РЕЗОЛВИНГ И ЗАПУСК ----------
def resolve_user(input_val):
    """Превращает screen_name или ID в numeric id (строку) и возвращает данные."""
    try:
        resp = vk.users.get(
            user_ids=str(input_val),
            fields='screen_name,first_name,last_name'
        )
        if resp:
            u = resp[0]
            return str(u['id']), u
    except Exception as e:
        print(f"Не удалось разрешить '{input_val}': {e}")
    return None, None


if __name__ == '__main__':
    print('=' * 60)
    print(f'Онлайн-трекинг: {ONLINE_USERS}')
    print(f'Глубокий трекинг: {DEEP_TRACK_USERS or "(отключён)"}')
    print(f'Логи: ./{LOG_DIR}')
    print('=' * 60)

    print('Резолвинг пользователей:')
    online_ids = []
    deep_ids = []

    for u in ONLINE_USERS:
        uid, udata = resolve_user(u)
        if uid:
            online_ids.append(uid)
            print(f"  [online] {uid} — {udata.get('first_name')} {udata.get('last_name')} (@{udata.get('screen_name')})")
        else:
            print(f"  [online] ОШИБКА для {u}")

    for u in DEEP_TRACK_USERS:
        uid, udata = resolve_user(u)
        if uid:
            deep_ids.append(uid)
            print(f"  [deep]   {uid} — {udata.get('first_name')} {udata.get('last_name')} (@{udata.get('screen_name')})")
        else:
            print(f"  [deep]   ОШИБКА для {u}")

    if not online_ids and not deep_ids:
        print('\nНе удалось разрешить ни одного пользователя. Проверьте токен и ID.')
        exit(1)
    print()

    if online_ids:
        threading.Thread(target=online_worker, args=(online_ids,), daemon=True).start()

    for uid in deep_ids:
        threading.Thread(target=likes_worker, args=(uid,), daemon=True).start()
        threading.Thread(target=comments_worker, args=(uid,), daemon=True).start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print('\nОстановлено.')