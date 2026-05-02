import glob
import json
import os
import random
import threading
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit
from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    ClientThrottledError,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    TwoFactorRequired,
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    logger=True,
    engineio_logger=True,
    ping_timeout=60,
    ping_interval=25,
)

bot_state = {
    "running": False,
    "feature_running": False,
    "cl": None,
    "username": None,
    "pending_cl": None,
    "pending_username": None,
    "pending_password": None,
    "stats": {
        "followers_gained": 0,
        "likes_given": 0,
        "unfollowed": 0,
        "stories_viewed": 0,
        "dms_sent": 0,
        "accounts_processed": 0,
        "start_time": None,
        "followbacks_received": 0,
    },
    "schedule_thread": None,
}

log_buffer = []
MAX_LOG_LINES = 300
BOT_DATA_FILE = os.path.join("data", "bot_data.json")

# ── Persistent data ───────────────────────────────────────────────────────────


def _default_bot_data():
    return {
        "stats": {
            "followers_gained": 0,
            "likes_given": 0,
            "unfollowed": 0,
            "stories_viewed": 0,
            "dms_sent": 0,
            "accounts_processed": 0,
            "start_time": None,
            "followbacks_received": 0,
        },
        "daily_snapshots": {},  # {"2026-04-28": {"followers_gained": 12, ...}}
        "follow_history": [],  # [{"uid": 123, "username": "x", "followed_at": "..."}]
        "schedule": {"enabled": False, "time_ranges": []},  # [{"start": "09:00", "stop": "12:00"}]
        "last_bot_settings": {"targets": "", "follow_limit": 120, "followers_per_account": 10},
    }


def load_bot_data():
    try:
        if os.path.exists(BOT_DATA_FILE):
            with open(BOT_DATA_FILE, "r") as f:
                data = json.load(f)
            # Merge in any new keys from defaults
            defaults = _default_bot_data()
            for k, v in defaults.items():
                if k not in data:
                    data[k] = v
                elif isinstance(v, dict) and isinstance(data[k], dict):
                    for dk, dv in v.items():
                        if dk not in data[k]:
                            data[k][dk] = dv
            return data
    except Exception as e:
        log(f"⚠️ Could not load bot data: {str(e)[:80]}")
    return _default_bot_data()


def save_bot_data():
    try:
        os.makedirs(os.path.dirname(BOT_DATA_FILE), exist_ok=True)
        data = {
            "stats": bot_state["stats"],
            "daily_snapshots": bot_data.get("daily_snapshots", {}),
            "follow_history": bot_data.get("follow_history", [])[-500:],  # cap size
            "schedule": bot_data.get("schedule", {"enabled": False, "time_ranges": []}),
            "last_bot_settings": bot_data.get("last_bot_settings", {"targets": "", "follow_limit": 120, "followers_per_account": 10}),
        }
        with open(BOT_DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"⚠️ Could not save bot data: {str(e)[:80]}")


def snapshot_today():
    today = datetime.now().strftime("%Y-%m-%d")
    snapshots = bot_data.setdefault("daily_snapshots", {})
    today_data = snapshots.get(today, {})
    for key in ["followers_gained", "likes_given", "unfollowed", "stories_viewed", "dms_sent", "followbacks_received"]:
        today_data[key] = today_data.get(key, 0)
    snapshots[today] = today_data
    save_bot_data()


bot_data = load_bot_data()
bot_state["stats"] = bot_data.get("stats", _default_bot_data()["stats"])

# ── Helpers ──────────────────────────────────────────────────────────────────


def log(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {message}"
    print(formatted, flush=True)  # shows in Railway/Render server logs
    log_buffer.append(formatted)
    if len(log_buffer) > MAX_LOG_LINES:
        del log_buffer[:-MAX_LOG_LINES]
    socketio.emit("log", {"message": formatted})


def make_client():
    cl = Client()
    cl.delay_range = [3, 7]
    proxy_url = os.environ.get("IG_PROXY_URL") or os.environ.get("PROXY_URL")
    if proxy_url:
        cl.set_proxy(proxy_url)
        log("🌐 Instagram proxy detected and enabled")
    else:
        log("⚠️ No IG_PROXY_URL set — Railway/Render IPs are often blocked by Instagram")
    return cl


def update_stats():
    socketio.emit("stats", bot_state["stats"])
    snapshot_today()
    save_bot_data()


def safe_delay(lo=3, hi=7):
    time.sleep(random.uniform(lo, hi))


# ── Session helpers ───────────────────────────────────────────────────────────


def load_saved_session():
    try:
        os.makedirs("sessions", exist_ok=True)
        files = glob.glob("sessions/*.json")
        main = [f for f in files if "backup" not in f]
        if not main:
            return False
        latest = max(main, key=os.path.getmtime)
        cl = make_client()
        cl.load_settings(latest)
        user_info = cl.account_info()
        username = user_info.username
        bot_state["cl"] = cl
        bot_state["username"] = username
        log(f"✅ Loaded saved session as @{username}")
        return True
    except Exception as e:
        log(f"⚠️ Saved session expired or invalid: {str(e)[:80]}")
        return False


def try_recover_session():
    try:
        log("🔄 Session expired — attempting auto-recovery...")
        files = glob.glob("sessions/*.json")
        if not files:
            log("❌ No saved sessions to recover from")
            bot_state["running"] = False
            socketio.emit("bot_status", {"running": False})
            socketio.emit("session_expired", {})
            return False
        latest = max(files, key=os.path.getmtime)
        cl = make_client()
        cl.load_settings(latest)
        cl.get_timeline_feed()
        user_info = cl.account_info()
        username = user_info.username
        bot_state["cl"] = cl
        bot_state["username"] = username
        cl.dump_settings(latest)
        log(f"✅ Session recovered as @{username}")
        return True
    except Exception as e:
        log(f"❌ Auto-recovery failed: {str(e)[:80]}")
        log("⚠️ Please re-login")
        bot_state["running"] = False
        socketio.emit("bot_status", {"running": False})
        socketio.emit("session_expired", {})
        return False


def handle_error(e, context="action"):
    if isinstance(e, LoginRequired):
        log(f"🔑 Session expired during {context}")
        return try_recover_session()
    elif isinstance(e, (PleaseWaitFewMinutes, ClientThrottledError)):
        log("⏳ Rate limited — waiting 5 minutes...")
        time.sleep(300)
        return True
    elif isinstance(e, ChallengeRequired):
        log("⚠️ Instagram challenge required — please verify your account manually")
        bot_state["running"] = False
        socketio.emit("bot_status", {"running": False})
        return False
    elif isinstance(e, FeedbackRequired):
        log("⚠️ Action blocked by Instagram — pausing 3 minutes...")
        time.sleep(180)
        return True
    else:
        err = str(e).lower()
        if "login_required" in err or "not authorized" in err:
            log(f"🔑 Login required during {context}")
            return try_recover_session()
        elif any(w in err for w in ["wait", "throttl", "rate", "few minutes"]):
            log("⏳ Rate limited — waiting 5 minutes...")
            time.sleep(300)
            return True
        elif "feedback_required" in err:
            log("⚠️ Action blocked — pausing 3 minutes...")
            time.sleep(180)
            return True
        log(f"⚠️ {context}: {str(e)[:100]}")
        return True


# ── Bot features ──────────────────────────────────────────────────────────────


def do_search_and_follow(target, max_followers, follow_limit, welcome_message=None):
    if not bot_state["cl"]:
        log("❌ Not logged in!")
        return
    try:
        cl = bot_state["cl"]
        log(f"🔍 Looking up @{target}...")
        user_id = cl.user_id_from_username(target)
        log(f"👥 Fetching up to {max_followers} followers...")
        followers = cl.user_followers(user_id, amount=max_followers)
        log(f"👥 Got {len(followers)} followers")

        followed = 0
        skipped = 0
        for uid, user_info in followers.items():
            if not bot_state["running"]:
                log("⏸️ Bot stopped")
                break
            if bot_state["stats"]["followers_gained"] >= follow_limit:
                log(f"🎯 Follow limit reached ({follow_limit})")
                break

            uname = getattr(user_info, "username", str(uid))

            # Follow
            try:
                cl = bot_state["cl"]
                cl.user_follow(int(uid))
                bot_state["stats"]["followers_gained"] += 1
                followed += 1
                # Record follow history for smart unfollow & follow-back tracking
                bot_data.setdefault("follow_history", []).append({
                    "uid": int(uid),
                    "username": uname,
                    "followed_at": datetime.now().isoformat(),
                })
                log(
                    f"✅ Followed @{uname} ({followed}/{max_followers}) | Total: {bot_state['stats']['followers_gained']}/{follow_limit}"
                )
                update_stats()
                time.sleep(random.uniform(8, 15))
            except Exception as e:
                skipped += 1
                if not handle_error(e, f"follow @{uname}"):
                    return
                continue

            # Welcome DM
            if welcome_message:
                try:
                    cl = bot_state["cl"]
                    dm_uid = cl.user_id_from_username(uname)
                    cl.direct_send(welcome_message, user_ids=[int(dm_uid)])
                    bot_state["stats"]["dms_sent"] += 1
                    log(f"💬 Welcome DM sent to @{uname}")
                    update_stats()
                    time.sleep(random.uniform(10, 20))
                except Exception as e:
                    if not handle_error(e, f"DM @{uname}"):
                        return

            if followed > 0 and followed % 5 == 0:
                pause = random.uniform(45, 90)
                log(f"😴 Anti-detection pause ({int(pause)}s)...")
                time.sleep(pause)

        bot_state["stats"]["accounts_processed"] += 1
        update_stats()
        log(f"✅ Done @{target}: {followed} followed, {skipped} skipped")
    except Exception as e:
        handle_error(e, f"search_and_follow @{target}")


def do_auto_unfollow(max_unfollows):
    if not bot_state["cl"]:
        log("❌ Not logged in!")
        return
    if bot_state["running"]:
        log("🛑 Stopping main bot to start Auto Unfollow...")
        bot_state["running"] = False
        socketio.emit("bot_status", {"running": False})
        time.sleep(2)
    bot_state["feature_running"] = True
    socketio.emit("feature_status", {"running": True, "name": "Auto Unfollow"})
    try:
        cl = bot_state["cl"]
        log(f"🔄 Fetching up to {max_unfollows} accounts to unfollow...")
        following = cl.user_following(cl.user_id, amount=max_unfollows)
        log(f"📊 Fetched {len(following)} accounts")
        unfollowed = 0
        skipped = 0
        for uid, user_info in following.items():
            if not bot_state["feature_running"]:
                log("⏹️ Auto Unfollow stopped")
                break
            if unfollowed >= max_unfollows:
                break
            uname = getattr(user_info, "username", str(uid))
            try:
                cl = bot_state["cl"]
                cl.user_unfollow(int(uid))
                unfollowed += 1
                bot_state["stats"]["unfollowed"] += 1
                log(f"✅ Unfollowed @{uname} ({unfollowed}/{max_unfollows})")
                update_stats()
                time.sleep(random.uniform(5, 10))
                if unfollowed % 10 == 0:
                    pause = random.uniform(45, 90)
                    log(f"😴 Pause ({int(pause)}s)...")
                    time.sleep(pause)
            except Exception as e:
                skipped += 1
                if not handle_error(e, f"unfollow @{uname}"):
                    break
        log(f"✅ Auto Unfollow done: {unfollowed} unfollowed, {skipped} skipped")
    except Exception as e:
        handle_error(e, "auto_unfollow")
    finally:
        bot_state["feature_running"] = False
        socketio.emit("feature_status", {"running": False, "name": "Auto Unfollow"})


def do_auto_like_feed(num_likes):
    if not bot_state["cl"]:
        log("❌ Not logged in!")
        return
    bot_state["feature_running"] = True
    socketio.emit("feature_status", {"running": True, "name": "Auto Like Feed"})
    try:
        cl = bot_state["cl"]
        log(f"❤️ Auto-liking feed (max {num_likes})...")
        feed = cl.get_timeline_feed()
        liked = 0
        for item in feed:
            if not bot_state["feature_running"] or liked >= num_likes:
                break
            try:
                pk = getattr(item, "pk", None) or getattr(item, "id", None)
                if not pk:
                    continue
                cl.media_like(pk)
                liked += 1
                bot_state["stats"]["likes_given"] += 1
                log(f"❤️ Liked #{liked}/{num_likes}")
                update_stats()
                time.sleep(random.uniform(3, 7))
            except Exception as e:
                if not handle_error(e, "like feed"):
                    break
        log(f"✅ Liked {liked} posts")
    except Exception as e:
        handle_error(e, "auto_like_feed")
    finally:
        bot_state["feature_running"] = False
        socketio.emit("feature_status", {"running": False, "name": "Auto Like Feed"})


def do_mass_story_view(max_stories, targets=""):
    if not bot_state["cl"]:
        log("❌ Not logged in!")
        return
    bot_state["feature_running"] = True
    socketio.emit("feature_status", {"running": True, "name": "Mass Story View"})
    try:
        cl = bot_state["cl"]
        log(f"👁️ Mass story view (max {max_stories}) — fast mode...")

        # Build list of user PKs to view stories from
        user_pks = []
        target_accounts = [t.strip() for t in targets.split(",") if t.strip()]
        if target_accounts:
            log(f"🎯 Fetching stories from target accounts: {', '.join('@' + t for t in target_accounts)}")
            for target in target_accounts:
                try:
                    uid = cl.user_id_from_username(target)
                    user_pks.append((uid, target))
                except Exception as e:
                    log(f"⚠️ Could not find @{target}: {str(e)[:60]}")
        else:
            log("📋 No targets specified — using timeline feed")
            feed = cl.get_timeline_feed()
            for item in feed:
                user_pk = getattr(getattr(item, "user", None), "pk", None)
                if user_pk:
                    user_pks.append((user_pk, None))

        viewed = 0
        for user_pk, uname in user_pks:
            if not bot_state["feature_running"] or viewed >= max_stories:
                break
            try:
                stories = cl.user_stories(user_pk)
                if not stories:
                    continue
                # Batch all story PKs for this user and mark seen at once
                story_pks = []
                for story in stories:
                    if viewed >= max_stories:
                        break
                    pk = getattr(story, "pk", None) or getattr(story, "id", None)
                    if pk:
                        story_pks.append(pk)
                        viewed += 1
                        bot_state["stats"]["stories_viewed"] += 1
                if story_pks:
                    cl.story_seen(story_pks)
                    label = f"@{uname}" if uname else f"user {user_pk}"
                    log(f"👁️ Viewed {len(story_pks)} stories from {label} ({viewed}/{max_stories})")
                    update_stats()
                # Tiny delay between users
                time.sleep(random.uniform(0.3, 0.8))
                # Small pause every 10 stories to avoid rate limits
                if viewed % 10 == 0:
                    log(f"😴 Brief pause at {viewed} stories...")
                    time.sleep(random.uniform(2, 5))
            except Exception:
                continue
        log(f"✅ Viewed {viewed} stories")
    except Exception as e:
        handle_error(e, "mass_story_view")
    finally:
        bot_state["feature_running"] = False
        socketio.emit("feature_status", {"running": False, "name": "Mass Story View"})


def do_auto_dm(target, message):
    if not bot_state["cl"]:
        log("❌ Not logged in!")
        return
    bot_state["feature_running"] = True
    try:
        cl = bot_state["cl"]
        log(f"💬 Sending DM to @{target}...")
        uid = cl.user_id_from_username(target)
        cl.direct_send(message, user_ids=[int(uid)])
        bot_state["stats"]["dms_sent"] += 1
        log(f"✅ DM sent to @{target}")
        update_stats()
    except Exception as e:
        handle_error(e, f"DM @{target}")
    finally:
        bot_state["feature_running"] = False


def do_auto_dm_following(message, limit):
    if not bot_state["cl"]:
        log("❌ Not logged in!")
        return
    bot_state["feature_running"] = True
    socketio.emit("feature_status", {"running": True, "name": "Auto DM"})
    try:
        cl = bot_state["cl"]
        log(f"💬 Auto-DM to following list (max {limit})...")
        following = cl.user_following(cl.user_id, amount=limit)
        log(f"📊 Got {len(following)} accounts")
        sent = 0
        for uid, user_info in following.items():
            if not bot_state["feature_running"] or sent >= limit:
                break
            uname = getattr(user_info, "username", str(uid))
            try:
                cl = bot_state["cl"]
                cl.direct_send(message, user_ids=[int(uid)])
                bot_state["stats"]["dms_sent"] += 1
                sent += 1
                log(f"✅ DM sent to @{uname} ({sent}/{limit})")
                update_stats()
                time.sleep(random.uniform(8, 15))
                if sent % 10 == 0:
                    pause = random.uniform(60, 120)
                    log(f"😴 Pause ({int(pause)}s)...")
                    time.sleep(pause)
            except Exception as e:
                if not handle_error(e, f"DM @{uname}"):
                    break
        log(f"✅ Auto DM done: {sent} sent")
    except Exception as e:
        handle_error(e, "auto_dm_following")
    finally:
        bot_state["feature_running"] = False
        socketio.emit("feature_status", {"running": False, "name": "Auto DM"})


def do_approve_requests():
    if not bot_state["cl"]:
        log("❌ Not logged in!")
        return
    bot_state["feature_running"] = True
    try:
        cl = bot_state["cl"]
        log("✅ Approving follow requests...")
        pending = cl.user_pending_follow_requests()
        approved = 0
        for req in pending:
            uid = getattr(req, "pk", None) or getattr(req, "id", None)
            uname = getattr(req, "username", str(uid))
            try:
                cl.approve_pending_follow_request(uid)
                approved += 1
                log(f"✅ Approved @{uname}")
                time.sleep(random.uniform(1, 3))
            except Exception as e:
                log(f"⚠️ Skip @{uname}: {str(e)[:50]}")
        log(f"✅ Approved {approved} requests")
    except Exception as e:
        handle_error(e, "approve_requests")
    finally:
        bot_state["feature_running"] = False


def do_like_user_posts(target, num_likes):
    if not bot_state["cl"]:
        log("❌ Not logged in!")
        return
    bot_state["feature_running"] = True
    socketio.emit("feature_status", {"running": True, "name": "Like User Posts"})
    try:
        cl = bot_state["cl"]
        log(f"❤️ Liking {num_likes} posts from @{target}...")
        user_id = cl.user_id_from_username(target)
        medias = cl.user_medias(user_id, amount=num_likes)
        liked = 0
        for media in medias:
            if not bot_state["feature_running"] or liked >= num_likes:
                break
            try:
                pk = getattr(media, "pk", None) or getattr(media, "id", None)
                cl.media_like(pk)
                liked += 1
                bot_state["stats"]["likes_given"] += 1
                log(f"❤️ Liked #{liked}/{num_likes} from @{target}")
                update_stats()
                time.sleep(random.uniform(3, 7))
            except Exception as e:
                if not handle_error(e, f"like post from @{target}"):
                    break
        log(f"✅ Liked {liked} posts from @{target}")
    except Exception as e:
        handle_error(e, f"like_user_posts @{target}")
    finally:
        bot_state["feature_running"] = False
        socketio.emit("feature_status", {"running": False, "name": "Like User Posts"})


def do_auto_comment(target, comments_list, limit):
    if not bot_state["cl"]:
        log("❌ Not logged in!")
        return
    bot_state["feature_running"] = True
    socketio.emit("feature_status", {"running": True, "name": "Auto Comment"})
    try:
        cl = bot_state["cl"]
        log(f"💬 Auto-commenting on @{target} (max {limit})...")
        user_id = cl.user_id_from_username(target)
        medias = cl.user_medias(user_id, amount=limit)
        commented = 0
        for media in medias:
            if not bot_state["feature_running"] or commented >= limit:
                break
            try:
                pk = getattr(media, "pk", None) or getattr(media, "id", None)
                comment = random.choice(comments_list)
                cl.media_comment(pk, comment)
                commented += 1
                log(f"💬 Commented '{comment}' on post #{commented}/{limit}")
                update_stats()
                time.sleep(random.uniform(5, 12))
            except Exception as e:
                if not handle_error(e, f"comment on @{target}"):
                    break
        log(f"✅ Commented on {commented} posts from @{target}")
    except Exception as e:
        handle_error(e, f"auto_comment @{target}")
    finally:
        bot_state["feature_running"] = False
        socketio.emit("feature_status", {"running": False, "name": "Auto Comment"})


def do_smart_unfollow(max_unfollows, min_days_following=3):
    """Unfollow only people who don't follow you back, with a grace period."""
    if not bot_state["cl"]:
        log("❌ Not logged in!")
        return
    if bot_state["running"]:
        log("🛑 Stopping main bot to start Smart Unfollow...")
        bot_state["running"] = False
        socketio.emit("bot_status", {"running": False})
        time.sleep(2)
    bot_state["feature_running"] = True
    socketio.emit("feature_status", {"running": True, "name": "Smart Unfollow"})
    try:
        cl = bot_state["cl"]
        log("📊 Fetching following and followers lists...")
        following = cl.user_following(cl.user_id)
        followers = cl.user_followers(cl.user_id)
        follower_ids = set(followers.keys())
        log(f"📊 You follow {len(following)} — {len(follower_ids)} follow you")

        # Build follow-history lookup for grace period
        history = bot_data.get("follow_history", [])
        followed_at_map = {}
        for entry in history:
            followed_at_map[int(entry["uid"])] = entry.get("followed_at", "")

        unfollowed = 0
        skipped = 0
        cutoff = datetime.now() - timedelta(days=min_days_following)

        for uid, user_info in following.items():
            if not bot_state["feature_running"] or unfollowed >= max_unfollows:
                break
            uid_int = int(uid)
            uname = getattr(user_info, "username", str(uid))

            # Skip if they follow back
            if uid_int in follower_ids or uid in follower_ids:
                continue

            # Grace period check — don't unfollow if followed too recently
            followed_at_str = followed_at_map.get(uid_int, "")
            if followed_at_str:
                try:
                    followed_at_dt = datetime.fromisoformat(followed_at_str)
                    if followed_at_dt > cutoff:
                        skipped += 1
                        continue  # too recent, give them more time
                except (ValueError, TypeError):
                    pass

            try:
                cl = bot_state["cl"]
                cl.user_unfollow(uid_int)
                unfollowed += 1
                bot_state["stats"]["unfollowed"] += 1
                log(f"✅ Unfollowed @{uname} (non-follower) ({unfollowed}/{max_unfollows})")
                update_stats()
                time.sleep(random.uniform(5, 10))
                if unfollowed % 10 == 0:
                    pause = random.uniform(45, 90)
                    log(f"😴 Pause ({int(pause)}s)...")
                    time.sleep(pause)
            except Exception as e:
                skipped += 1
                if not handle_error(e, f"smart unfollow @{uname}"):
                    break

        log(f"✅ Smart Unfollow done: {unfollowed} unfollowed, {skipped} skipped (grace: {min_days_following}d)")
    except Exception as e:
        handle_error(e, "smart_unfollow")
    finally:
        bot_state["feature_running"] = False
        socketio.emit("feature_status", {"running": False, "name": "Smart Unfollow"})


def do_hashtag_follow(hashtag, follow_limit, welcome_message=None):
    """Follow likers of recent posts under a hashtag."""
    if not bot_state["cl"]:
        log("❌ Not logged in!")
        return
    bot_state["feature_running"] = True
    socketio.emit("feature_status", {"running": True, "name": "Hashtag Follow"})
    try:
        cl = bot_state["cl"]
        log(f"🔍 Fetching recent posts for #{hashtag}...")
        medias = cl.hashtag_medias_recent(hashtag, amount=20)
        log(f"📸 Got {len(medias)} recent posts for #{hashtag}")

        followed = 0
        for media in medias:
            if not bot_state["feature_running"] or followed >= follow_limit:
                break
            try:
                pk = getattr(media, "pk", None) or getattr(media, "id", None)
                if not pk:
                    continue
                log(f"❤️ Fetching likers for post {pk}...")
                likers = cl.media_likers(pk)
                log(f"👥 Got {len(likers)} likers")

                for liker in likers:
                    if not bot_state["feature_running"] or followed >= follow_limit:
                        break
                    try:
                        liker_uid = getattr(liker, "pk", None) or getattr(liker, "id", None)
                        liker_uname = getattr(liker, "username", str(liker_uid))
                        if not liker_uid:
                            continue
                        cl = bot_state["cl"]
                        cl.user_follow(int(liker_uid))
                        followed += 1
                        bot_state["stats"]["followers_gained"] += 1
                        bot_data.setdefault("follow_history", []).append({
                            "uid": int(liker_uid),
                            "username": liker_uname,
                            "followed_at": datetime.now().isoformat(),
                        })
                        log(f"✅ Followed @{liker_uname} via #{hashtag} ({followed}/{follow_limit})")
                        update_stats()
                        time.sleep(random.uniform(8, 15))

                        # Welcome DM
                        if welcome_message:
                            try:
                                cl.direct_send(welcome_message, user_ids=[int(liker_uid)])
                                bot_state["stats"]["dms_sent"] += 1
                                log(f"💬 Welcome DM sent to @{liker_uname}")
                                update_stats()
                                time.sleep(random.uniform(10, 20))
                            except Exception as e:
                                if not handle_error(e, f"DM @{liker_uname}"):
                                    return

                        if followed % 5 == 0:
                            pause = random.uniform(45, 90)
                            log(f"😴 Anti-detection pause ({int(pause)}s)...")
                            time.sleep(pause)
                    except Exception as e:
                        if not handle_error(e, f"follow @{getattr(liker, 'username', '?')}"):
                            break
                        continue

                time.sleep(random.uniform(5, 10))
            except Exception as e:
                if not handle_error(e, f"hashtag media {pk}"):
                    break
                continue

        log(f"✅ Hashtag follow done: {followed} followed via #{hashtag}")
    except Exception as e:
        handle_error(e, f"hashtag_follow #{hashtag}")
    finally:
        bot_state["feature_running"] = False
        socketio.emit("feature_status", {"running": False, "name": "Hashtag Follow"})


def do_check_followback():
    """Check how many people you followed have followed you back."""
    if not bot_state["cl"]:
        log("❌ Not logged in!")
        return
    bot_state["feature_running"] = True
    socketio.emit("feature_status", {"running": True, "name": "Follow-back Check"})
    try:
        cl = bot_state["cl"]
        log("📊 Checking follow-back rate...")
        following = cl.user_following(cl.user_id)
        followers = cl.user_followers(cl.user_id)
        follower_ids = set(followers.keys())

        history = bot_data.get("follow_history", [])
        bot_followed_uids = set(int(e["uid"]) for e in history[-200:])

        followbacks = 0
        checked = 0
        for uid in bot_followed_uids:
            if uid in follower_ids or str(uid) in follower_ids:
                followbacks += 1
            checked += 1

        bot_state["stats"]["followbacks_received"] = followbacks
        rate = (followbacks / checked * 100) if checked > 0 else 0
        log(f"📊 Follow-back rate: {followbacks}/{checked} ({rate:.1f}%)")
        update_stats()
        socketio.emit("followback_stats", {
            "followbacks": followbacks,
            "checked": checked,
            "rate": round(rate, 1),
        })
    except Exception as e:
        handle_error(e, "check_followback")
    finally:
        bot_state["feature_running"] = False
        socketio.emit("feature_status", {"running": False, "name": "Follow-back Check"})


def _schedule_loop():
    """Background thread that checks schedule and auto-starts/stops the bot."""
    while True:
        schedule_cfg = bot_data.get("schedule", {})
        if not schedule_cfg.get("enabled"):
            time.sleep(60)
            continue

        now = datetime.now()
        current_time = now.strftime("%H:%M")
        in_window = False
        for tr in schedule_cfg.get("time_ranges", []):
            start = tr.get("start", "00:00")
            stop = tr.get("stop", "00:00")
            if start <= current_time < stop:
                in_window = True
                break

        if in_window and not bot_state["running"] and bot_state["cl"]:
            # Random jitter so bot doesn't always start at exact same second
            jitter = random.uniform(10, 120)
            log(f"⏰ Schedule: window active at {current_time} — starting in {int(jitter)}s...")
            time.sleep(jitter)
            # Re-check in case user manually stopped during jitter
            if not bot_state["running"] and bot_state["cl"]:
                saved = bot_data.get("last_bot_settings", {})
                targets = saved.get("targets", "")
                follow_limit = saved.get("follow_limit", 120)
                fpa = saved.get("followers_per_account", 10)
                if not targets.strip():
                    log("⚠️ Schedule: no target accounts saved — set targets and start bot once first")
                else:
                    log(f"⏰ Schedule: auto-starting with targets: {targets}")
                    bot_state["running"] = True
                    bot_state["stats"]["start_time"] = time.time()
                    socketio.emit("bot_status", {"running": True})
                    threading.Thread(
                        target=run_bot_loop,
                        args=(targets, follow_limit, fpa, None),
                        daemon=True,
                    ).start()
        elif not in_window and bot_state["running"]:
            log(f"⏰ Schedule: stopping bot (outside window at {current_time})")
            bot_state["running"] = False
            socketio.emit("bot_status", {"running": False})

        time.sleep(60)  # check every minute


# Start schedule thread on boot
threading.Thread(target=_schedule_loop, daemon=True).start()


# ── Main bot loop ─────────────────────────────────────────────────────────────


def run_bot_loop(targets, follow_limit, followers_per_account, welcome_message=None):
    try:
        log("🚀 Starting bot loop...")
        if not bot_state["cl"]:
            log("❌ Not logged in!")
            bot_state["running"] = False
            socketio.emit("bot_status", {"running": False})
            return

        log(f"✅ Running as @{bot_state['username']}")
        target_accounts = [t.strip() for t in targets.split(",") if t.strip()]
        if not target_accounts:
            log("❌ No target accounts specified!")
            bot_state["running"] = False
            socketio.emit("bot_status", {"running": False})
            return

        log(f"🎯 Targets: {', '.join('@' + t for t in target_accounts)}")
        log(f"📊 Follow limit: {follow_limit} | Per account: {followers_per_account}")
        if welcome_message:
            log(f"💬 Welcome DM enabled")

        loop_count = 0
        while (
            bot_state["running"]
            and bot_state["stats"]["followers_gained"] < follow_limit
        ):
            loop_count += 1
            log(f"🔄 === Loop #{loop_count} ===")
            random.shuffle(target_accounts)
            for target in target_accounts:
                if not bot_state["running"]:
                    break
                if bot_state["stats"]["followers_gained"] >= follow_limit:
                    break
                log(
                    f"📊 Progress: {bot_state['stats']['followers_gained']}/{follow_limit}"
                )
                do_search_and_follow(
                    target, followers_per_account, follow_limit, welcome_message
                )
                safe_delay(3, 8)

        log("🎉 Bot session completed!")
        log(f"📊 Final: {bot_state['stats']['followers_gained']} followers gained")
    except Exception as e:
        log(f"❌ Bot error: {str(e)}")
    finally:
        bot_state["running"] = False
        socketio.emit("bot_status", {"running": False})


# ── Flask routes ──────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return {"status": "ok", "logged_in": bot_state["username"] is not None}


@app.route("/api/status")
def api_status():
    return jsonify(
        {
            "ok": True,
            "running": bot_state["running"],
            "feature_running": bot_state["feature_running"],
            "logged_in": bot_state["username"] is not None,
            "username": bot_state["username"],
            "stats": bot_state["stats"],
        }
    )


@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": log_buffer[-MAX_LOG_LINES:]})


@app.route("/api/daily_stats")
def api_daily_stats():
    return jsonify(bot_data.get("daily_snapshots", {}))


# ── Socket events ─────────────────────────────────────────────────────────────


@socketio.on("connect")
def on_connect():
    for line in log_buffer[-80:]:
        emit("log", {"message": line})
    emit("bot_status", {"running": bot_state["running"]})
    emit("feature_status", {"running": bot_state["feature_running"], "name": "Feature"})
    emit("stats", bot_state["stats"])
    if not bot_state["cl"]:
        if load_saved_session():
            emit(
                "login_status",
                {"success": True, "username": bot_state.get("username", "")},
            )
        else:
            emit(
                "login_status",
                {"success": False, "error": "No saved session — please log in"},
            )


@socketio.on("login")
def on_login(data):
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        emit("login_status", {"success": False, "error": "Enter username and password"})
        return

    def _login():
        try:
            log(f"🔐 Login attempt for @{username}...")
            cl = make_client()
            bot_state["pending_cl"] = cl
            bot_state["pending_username"] = username
            bot_state["pending_password"] = password
            try:
                cl.login(username, password)
            except TwoFactorRequired:
                log(f"🔐 2FA required for @{username}")
                socketio.emit("two_fa_required", {"required": True})
                return
            except ChallengeRequired:
                log(
                    "⚠️ Instagram challenge required. Open Instagram on your phone/browser, verify the login, then try again."
                )
                socketio.emit(
                    "login_status",
                    {
                        "success": False,
                        "error": "Instagram challenge required — verify in Instagram, then try again.",
                    },
                )
                return
            except Exception as e:
                err = str(e).lower()
                if any(w in err for w in ["two", "factor", "verification", "code"]):
                    log(f"🔐 2FA required for @{username}")
                    socketio.emit("two_fa_required", {"required": True})
                    return
                if any(
                    w in err
                    for w in [
                        "challenge",
                        "checkpoint",
                        "suspicious",
                        "verify",
                        "blacklis",
                        "ip address",
                        "linked facebook",
                    ]
                ):
                    log(
                        "🚫 Instagram rejected this login because the Railway/cloud IP is blocked. Add IG_PROXY_URL with a residential/mobile proxy, then redeploy."
                    )
                    socketio.emit(
                        "login_status",
                        {
                            "success": False,
                            "error": "Instagram blocked Railway's IP. Add IG_PROXY_URL with a residential/mobile proxy, then redeploy.",
                        },
                    )
                    return
                raise
            os.makedirs("sessions", exist_ok=True)
            cl.dump_settings(f"sessions/{username}.json")
            bot_state["cl"] = cl
            bot_state["username"] = username
            log(f"✅ Logged in as @{username}")
            socketio.emit("login_status", {"success": True, "username": username})
        except Exception as e:
            err_msg = str(e)[:160]
            log(f"❌ Login failed: {err_msg}")
            socketio.emit("login_status", {"success": False, "error": err_msg})

    threading.Thread(target=_login, daemon=True).start()


@socketio.on("password_login")
def on_password_login(data):
    # Backward compatibility for older frontend builds/cached browsers.
    return on_login(data)


@socketio.on("browser_login")
def on_browser_login(data=None):
    emit(
        "login_status",
        {
            "success": False,
            "error": "Browser login is not available on Railway. Use username/password login.",
        },
    )


@socketio.on("two_fa_code")
def on_two_fa(data):
    code = data.get("code", "").strip()
    if not code:
        emit("login_status", {"success": False, "error": "Enter the 2FA code"})
        return

    def _submit():
        try:
            cl = bot_state.get("pending_cl")
            username = bot_state.get("pending_username")
            password = bot_state.get("pending_password")
            if not cl or not username:
                socketio.emit(
                    "login_status",
                    {"success": False, "error": "Session lost — please log in again"},
                )
                return
            cl.login(username, password, verification_code=code)
            os.makedirs("sessions", exist_ok=True)
            cl.dump_settings(f"sessions/{username}.json")
            bot_state["cl"] = cl
            bot_state["username"] = username
            log(f"✅ 2FA login successful as @{username}")
            socketio.emit("login_status", {"success": True, "username": username})
            socketio.emit("two_fa_required", {"required": False})
        except Exception as e:
            log(f"❌ 2FA failed: {str(e)[:100]}")
            socketio.emit(
                "login_status", {"success": False, "error": "Wrong code — try again"}
            )

    threading.Thread(target=_submit, daemon=True).start()


@socketio.on("start_bot")
def on_start(data):
    if bot_state["running"]:
        emit("bot_status", {"running": True})
        return
    if not bot_state["cl"]:
        emit("login_status", {"success": False, "error": "Please log in first"})
        return
    bot_state["running"] = True
    bot_state["feature_running"] = False
    bot_state["stats"] = {
        "followers_gained": 0,
        "likes_given": 0,
        "unfollowed": 0,
        "stories_viewed": 0,
        "dms_sent": 0,
        "accounts_processed": 0,
        "start_time": time.time(),
        "followbacks_received": 0,
    }
    dm_enabled = data.get("dm_enabled", False)
    welcome_msg = data.get("welcome_message", "").strip() if dm_enabled else ""
    # Save settings so scheduler can reuse them
    bot_data["last_bot_settings"] = {
        "targets": data.get("targets", ""),
        "follow_limit": int(data.get("follow_limit", 120)),
        "followers_per_account": int(data.get("followers_per_account", 10)),
    }
    save_bot_data()
    threading.Thread(
        target=run_bot_loop,
        args=(
            data.get("targets", ""),
            int(data.get("follow_limit", 120)),
            int(data.get("followers_per_account", 10)),
            welcome_msg or None,
        ),
        daemon=True,
    ).start()
    emit("bot_status", {"running": True})


@socketio.on("stop_bot")
def on_stop():
    bot_state["running"] = False
    emit("bot_status", {"running": False})
    log("🛑 Bot stopped")


@socketio.on("stop_feature")
def on_stop_feature():
    bot_state["feature_running"] = False
    log("🛑 Feature stopped")


@socketio.on("auto_unfollow")
def on_unfollow(data):
    if not bot_state["cl"]:
        emit("login_status", {"success": False, "error": "Please log in first"})
        return
    if bot_state["feature_running"]:
        bot_state["feature_running"] = False
        time.sleep(1)
    limit = int(data.get("limit", 50))
    threading.Thread(target=do_auto_unfollow, args=(limit,), daemon=True).start()


@socketio.on("auto_like_feed")
def on_like_feed(data):
    if not bot_state["cl"]:
        log("❌ Please log in to Instagram first")
        emit("login_status", {"success": False, "error": "Please log in first"})
        return
    threading.Thread(
        target=do_auto_like_feed, args=(int(data.get("limit", 20)),), daemon=True
    ).start()


@socketio.on("mass_story_view")
def on_story(data):
    if not bot_state["cl"]:
        log("❌ Please log in to Instagram first")
        emit("login_status", {"success": False, "error": "Please log in first"})
        return
    threading.Thread(
        target=do_mass_story_view, args=(int(data.get("limit", 50)), data.get("targets", "")), daemon=True
    ).start()


@socketio.on("auto_dm")
def on_dm(data):
    if not bot_state["cl"]:
        log("❌ Please log in to Instagram first")
        emit("login_status", {"success": False, "error": "Please log in first"})
        return
    msg = data.get("message", "") or "Hey! Thanks for following 🙏"
    target = data.get("target", "").strip()
    limit = int(data.get("limit", 20))
    if target:
        threading.Thread(target=do_auto_dm, args=(target, msg), daemon=True).start()
    else:
        threading.Thread(
            target=do_auto_dm_following, args=(msg, limit), daemon=True
        ).start()


@socketio.on("approve_requests")
def on_approve():
    if not bot_state["cl"]:
        log("❌ Please log in to Instagram first")
        emit("login_status", {"success": False, "error": "Please log in first"})
        return
    threading.Thread(target=do_approve_requests, daemon=True).start()


@socketio.on("like_user_posts")
def on_like_user(data):
    if not bot_state["cl"]:
        log("❌ Please log in to Instagram first")
        emit("login_status", {"success": False, "error": "Please log in first"})
        return
    target = data.get("target", "").strip()
    if not target:
        log("❌ Enter a username in Like Target")
        return
    threading.Thread(
        target=do_like_user_posts, args=(target, int(data.get("limit", 5))), daemon=True
    ).start()


@socketio.on("auto_comment")
def on_comment(data):
    if not bot_state["cl"]:
        log("❌ Please log in to Instagram first")
        emit("login_status", {"success": False, "error": "Please log in first"})
        return
    target = data.get("target", "").strip()
    if not target:
        log("❌ Enter a target username for Auto Comment")
        return
    raw = data.get("comments", "Great post!, 🔥, Amazing!")
    comments_list = [c.strip() for c in raw.split(",") if c.strip()] or ["🔥"]
    limit = int(data.get("limit", 10))
    threading.Thread(
        target=do_auto_comment, args=(target, comments_list, limit), daemon=True
    ).start()


@socketio.on("welcome_dm")
def on_welcome_dm(data):
    if not bot_state["cl"]:
        emit("login_status", {"success": False, "error": "Please log in first"})
        return
    msg = data.get("message", "").strip() or "Hey! Thanks for following me 🙏"
    limit = int(data.get("limit", 20))
    threading.Thread(
        target=do_auto_dm_following, args=(msg, limit), daemon=True
    ).start()


@socketio.on("smart_unfollow")
def on_smart_unfollow(data):
    if not bot_state["cl"]:
        emit("login_status", {"success": False, "error": "Please log in first"})
        return
    if bot_state["feature_running"]:
        bot_state["feature_running"] = False
        time.sleep(1)
    limit = int(data.get("limit", 50))
    grace_days = int(data.get("grace_days", 3))
    threading.Thread(target=do_smart_unfollow, args=(limit, grace_days), daemon=True).start()


@socketio.on("hashtag_follow")
def on_hashtag_follow(data):
    if not bot_state["cl"]:
        emit("login_status", {"success": False, "error": "Please log in first"})
        return
    hashtag = data.get("hashtag", "").strip().lstrip("#")
    if not hashtag:
        log("❌ Enter a hashtag for Hashtag Follow")
        return
    limit = int(data.get("limit", 30))
    welcome_msg = data.get("welcome_message", "").strip() or None
    threading.Thread(target=do_hashtag_follow, args=(hashtag, limit, welcome_msg), daemon=True).start()


@socketio.on("check_followback")
def on_check_followback():
    if not bot_state["cl"]:
        emit("login_status", {"success": False, "error": "Please log in first"})
        return
    threading.Thread(target=do_check_followback, daemon=True).start()


@socketio.on("set_schedule")
def on_set_schedule(data):
    enabled = data.get("enabled", False)
    time_ranges = data.get("time_ranges", [])
    # Validate time ranges
    valid_ranges = []
    for tr in time_ranges:
        start = tr.get("start", "").strip()
        stop = tr.get("stop", "").strip()
        if start and stop:
            valid_ranges.append({"start": start, "stop": stop})
    bot_data["schedule"] = {"enabled": enabled, "time_ranges": valid_ranges}
    save_bot_data()
    status = "enabled" if enabled else "disabled"
    log(f"⏰ Schedule {status}: {valid_ranges}")
    socketio.emit("schedule_status", {"enabled": enabled, "time_ranges": valid_ranges})


@socketio.on("get_schedule")
def on_get_schedule():
    schedule_cfg = bot_data.get("schedule", {"enabled": False, "time_ranges": []})
    emit("schedule_status", schedule_cfg)


@socketio.on("get_daily_stats")
def on_get_daily_stats():
    snapshots = bot_data.get("daily_snapshots", {})
    emit("daily_stats", snapshots)


@socketio.on("reset_stats")
def on_reset():
    bot_state["stats"] = {
        "followers_gained": 0,
        "likes_given": 0,
        "unfollowed": 0,
        "stories_viewed": 0,
        "dms_sent": 0,
        "accounts_processed": 0,
        "start_time": None,
        "followbacks_received": 0,
    }
    emit("stats", bot_state["stats"])
    save_bot_data()


log("🚀 Instagram Bot starting up...")
log(f"🌍 Environment: {os.environ.get('FLASK_ENV', 'production')}")
log(f"📁 Sessions dir: {os.path.abspath('sessions')}")
os.makedirs("sessions", exist_ok=True)
os.makedirs("data", exist_ok=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    debug = os.environ.get("FLASK_ENV", "production") != "production"
    log(f"🔌 Starting on port {port}")
    socketio.run(
        app, host="0.0.0.0", port=port, debug=debug, allow_unsafe_werkzeug=True
    )
