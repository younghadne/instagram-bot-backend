# Instagram Bot Backend — Sample Implementation Reference
# This is a reference implementation showing the structure and key functions.
# For production use, implement the full version with all error handling and features.

from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit, disconnect
from instagrapi import Client
import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
import random
import logging

# Configuration
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key')
socketio = SocketIO(app, cors_allowed_origins="*")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Paths
SESSIONS_DIR = Path('sessions')
DATA_DIR = Path('data')
SESSIONS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# Global state
bot_state = {
    'running': False,
    'feature_running': False,
    'logged_in': False,
    'username': None,
    'client': None,
    'logs': [],
    'stats': {
        'followers_gained': 0,
        'likes_given': 0,
        'unfollowed': 0,
        'stories_viewed': 0,
        'dms_sent': 0,
        'follow_backs': 0,
    }
}


class InstagramBot:
    """Main bot class handling all Instagram operations."""
    
    def __init__(self):
        self.client = None
        self.username = None
        self.proxy = os.getenv('IG_PROXY_URL') or os.getenv('PROXY_URL')
    
    def log(self, message):
        """Log message and emit to frontend."""
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_message = f"[{timestamp}] {message}"
        bot_state['logs'].append(log_message)
        socketio.emit('log', {'message': log_message})
        logger.info(message)
    
    def browser_login(self):
        """Initiate browser-based Instagram login."""
        try:
            self.client = Client()
            if self.proxy:
                self.client.set_proxy(self.proxy)
            
            # Browser login flow
            self.client.login_with_browser()
            self.username = self.client.username
            bot_state['logged_in'] = True
            bot_state['username'] = self.username
            
            # Save session
            self._save_session()
            self.log(f"✅ Browser login successful as @{self.username}")
            socketio.emit('login_status', {'logged_in': True, 'username': self.username})
            return True
        except Exception as e:
            self.log(f"❌ Browser login failed: {str(e)}")
            return False
    
    def password_login(self, username, password, verification_code=None):
        """Login with username and password."""
        try:
            self.client = Client()
            if self.proxy:
                self.client.set_proxy(self.proxy)
            
            # Attempt login
            self.client.login(username, password, verification_code=verification_code)
            self.username = username
            bot_state['logged_in'] = True
            bot_state['username'] = self.username
            
            # Save session
            self._save_session()
            self.log(f"✅ Password login successful as @{username}")
            socketio.emit('login_status', {'logged_in': True, 'username': username})
            return True
        except Exception as e:
            if '2fa' in str(e).lower() or 'verification' in str(e).lower():
                self.log("🔐 2FA code required")
                socketio.emit('two_fa_required', {'message': 'Enter 2FA code'})
            else:
                self.log(f"❌ Login failed: {str(e)}")
            return False
    
    def _save_session(self):
        """Save session to file."""
        if self.client and self.username:
            session_file = SESSIONS_DIR / f"{self.username}.json"
            try:
                # Save session data
                session_data = self.client.get_settings()
                with open(session_file, 'w') as f:
                    json.dump(session_data, f)
                self.log(f"💾 Session saved for @{self.username}")
            except Exception as e:
                self.log(f"⚠️ Failed to save session: {str(e)}")
    
    def _load_session(self, username):
        """Load session from file."""
        session_file = SESSIONS_DIR / f"{username}.json"
        if session_file.exists():
            try:
                self.client = Client()
                if self.proxy:
                    self.client.set_proxy(self.proxy)
                
                with open(session_file, 'r') as f:
                    session_data = json.load(f)
                
                # Restore session
                self.client.set_settings(session_data)
                self.username = username
                bot_state['logged_in'] = True
                self.log(f"✅ Session restored for @{username}")
                return True
            except Exception as e:
                self.log(f"⚠️ Failed to restore session: {str(e)}")
                return False
        return False
    
    def run_bot_loop(self, targets, follow_limit, followers_per_account, welcome_message):
        """Main bot follow loop."""
        if not self.client or not bot_state['logged_in']:
            self.log("❌ Not logged in")
            return
        
        bot_state['running'] = True
        self.log("🤖 Bot started")
        
        try:
            target_list = [t.strip() for t in targets.split(',')]
            total_followed = 0
            
            for target in target_list:
                if not bot_state['running']:
                    break
                
                self.log(f"🎯 Targeting @{target}")
                
                try:
                    # Get target user info
                    user = self.client.user_info_by_username(target)
                    followers = self.client.user_followers(user.pk, amount=followers_per_account)
                    
                    for follower in followers:
                        if not bot_state['running'] or total_followed >= follow_limit:
                            break
                        
                        try:
                            # Follow user
                            self.client.user_follow(follower.pk)
                            total_followed += 1
                            bot_state['stats']['followers_gained'] += 1
                            self.log(f"✅ Followed @{follower.username}")
                            
                            # Send welcome DM if configured
                            if welcome_message:
                                self.client.direct_send(welcome_message, [follower.pk])
                                bot_state['stats']['dms_sent'] += 1
                            
                            # Anti-detection delay
                            delay = random.uniform(8, 15)
                            time.sleep(delay)
                            
                            # Pause every 5 follows
                            if total_followed % 5 == 0:
                                pause = random.uniform(45, 90)
                                self.log(f"⏸️ Pausing for {pause:.0f}s")
                                time.sleep(pause)
                        
                        except Exception as e:
                            self.log(f"⚠️ Error following @{follower.username}: {str(e)}")
                            self._handle_error(e)
                
                except Exception as e:
                    self.log(f"❌ Error targeting @{target}: {str(e)}")
            
            self.log(f"✅ Bot loop completed. Followed {total_followed} accounts")
        
        except Exception as e:
            self.log(f"❌ Bot error: {str(e)}")
        
        finally:
            bot_state['running'] = False
            self._save_bot_data()
            socketio.emit('bot_status', {'running': False})
    
    def auto_unfollow(self, unfollow_limit):
        """Unfollow accounts from following list."""
        if not self.client or not bot_state['logged_in']:
            self.log("❌ Not logged in")
            return
        
        bot_state['feature_running'] = True
        self.log(f"🔄 Starting auto-unfollow (limit: {unfollow_limit})")
        
        try:
            user = self.client.user_info(self.client.user_id)
            following = self.client.user_following(user.pk, amount=unfollow_limit)
            
            unfollowed = 0
            for user_to_unfollow in following:
                if not bot_state['feature_running'] or unfollowed >= unfollow_limit:
                    break
                
                try:
                    self.client.user_unfollow(user_to_unfollow.pk)
                    unfollowed += 1
                    bot_state['stats']['unfollowed'] += 1
                    self.log(f"✅ Unfollowed @{user_to_unfollow.username}")
                    time.sleep(random.uniform(3, 8))
                
                except Exception as e:
                    self.log(f"⚠️ Error unfollowing: {str(e)}")
            
            self.log(f"✅ Unfollowed {unfollowed} accounts")
        
        except Exception as e:
            self.log(f"❌ Auto-unfollow error: {str(e)}")
        
        finally:
            bot_state['feature_running'] = False
            self._save_bot_data()
    
    def auto_like_feed(self, like_limit):
        """Like posts from timeline."""
        if not self.client or not bot_state['logged_in']:
            self.log("❌ Not logged in")
            return
        
        bot_state['feature_running'] = True
        self.log(f"❤️ Starting auto-like (limit: {like_limit})")
        
        try:
            feed = self.client.get_timeline_feed()
            liked = 0
            
            for media in feed:
                if not bot_state['feature_running'] or liked >= like_limit:
                    break
                
                try:
                    if not media.has_liked:
                        self.client.media_like(media.pk)
                        liked += 1
                        bot_state['stats']['likes_given'] += 1
                        self.log(f"❤️ Liked post by @{media.user.username}")
                        time.sleep(random.uniform(2, 5))
                
                except Exception as e:
                    self.log(f"⚠️ Error liking post: {str(e)}")
            
            self.log(f"✅ Liked {liked} posts")
        
        except Exception as e:
            self.log(f"❌ Auto-like error: {str(e)}")
        
        finally:
            bot_state['feature_running'] = False
            self._save_bot_data()
    
    def _handle_error(self, error):
        """Handle Instagram API errors."""
        error_str = str(error).lower()
        
        if 'loginrequired' in error_str:
            self.log("🔐 Login required - attempting session recovery")
            bot_state['running'] = False
        elif 'pleasewait' in error_str or 'throttled' in error_str:
            self.log("⏱️ Rate limited - waiting 5 minutes")
            time.sleep(300)
        elif 'challenge' in error_str:
            self.log("⚠️ Challenge required - bot stopped")
            bot_state['running'] = False
        elif 'feedback' in error_str:
            self.log("⏸️ Feedback required - pausing 3 minutes")
            time.sleep(180)
    
    def _save_bot_data(self):
        """Save bot data to JSON file."""
        data_file = DATA_DIR / 'bot_data.json'
        try:
            data = {
                'stats': bot_state['stats'],
                'timestamp': datetime.now().isoformat(),
            }
            with open(data_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.log(f"⚠️ Failed to save bot data: {str(e)}")


# Initialize bot
bot = InstagramBot()


# REST Endpoints
@app.route('/')
def index():
    """Serve dashboard."""
    return render_template('index.html')


@app.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({'status': 'ok', 'timestamp': datetime.now().isoformat()})


@app.route('/api/status')
def get_status():
    """Get bot status."""
    return jsonify({
        'running': bot_state['running'],
        'logged_in': bot_state['logged_in'],
        'username': bot_state['username'],
        'stats': bot_state['stats'],
    })


@app.route('/api/logs')
def get_logs():
    """Get last 300 logs."""
    return jsonify({'logs': bot_state['logs'][-300:]})


# Socket.IO Events
@socketio.on('connect')
def handle_connect():
    """Client connected."""
    logger.info('Client connected')
    emit('login_status', {'logged_in': bot_state['logged_in'], 'username': bot_state['username']})


@socketio.on('browser_login')
def handle_browser_login():
    """Handle browser login request."""
    threading.Thread(target=bot.browser_login, daemon=True).start()


@socketio.on('password_login')
def handle_password_login(data):
    """Handle password login request."""
    username = data.get('username')
    password = data.get('password')
    verification_code = data.get('verification_code')
    threading.Thread(
        target=bot.password_login,
        args=(username, password, verification_code),
        daemon=True
    ).start()


@socketio.on('start_bot')
def handle_start_bot(data):
    """Start bot with settings."""
    targets = data.get('targets', '')
    follow_limit = data.get('follow_limit', 200)
    followers_per_account = data.get('followers_per_account', 50)
    welcome_message = data.get('welcome_message', '')
    
    threading.Thread(
        target=bot.run_bot_loop,
        args=(targets, follow_limit, followers_per_account, welcome_message),
        daemon=True
    ).start()
    
    emit('bot_status', {'running': True})


@socketio.on('stop_bot')
def handle_stop_bot():
    """Stop bot."""
    bot_state['running'] = False
    bot.log("⏹️ Bot stopped by user")
    emit('bot_status', {'running': False})


@socketio.on('auto_unfollow')
def handle_auto_unfollow(data):
    """Handle auto-unfollow request."""
    unfollow_limit = data.get('unfollow_limit', 100)
    threading.Thread(
        target=bot.auto_unfollow,
        args=(unfollow_limit,),
        daemon=True
    ).start()


@socketio.on('auto_like_feed')
def handle_auto_like_feed(data):
    """Handle auto-like request."""
    like_limit = data.get('like_limit', 150)
    threading.Thread(
        target=bot.auto_like_feed,
        args=(like_limit,),
        daemon=True
    ).start()


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.getenv('PORT', 10000)), debug=False)

