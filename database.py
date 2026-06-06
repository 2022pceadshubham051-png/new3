import sqlite3
import time
import os
import logging

DB_FILE = "musicverse.db"

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        
        # User details and listening time
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            total_listen_seconds INTEGER DEFAULT 0
        )
        """)
        
        # Song play count stats per user
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS song_stats (
            user_id INTEGER,
            song_title TEXT,
            play_count INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, song_title)
        )
        """)
        
        # Authorized users per group chat
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS auth_users (
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
        """)
        
        # Approved members (global authorization by bot owner)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS approved_members (
            user_id INTEGER PRIMARY KEY,
            username TEXT
        )
        """)
        
        # Admin cache per chat
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin_cache (
            chat_id INTEGER,
            user_id INTEGER,
            cached_at REAL,
            PRIMARY KEY (chat_id, user_id)
        )
        """)
        
        # Tracked chats (for broadcasting)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracked_chats (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            added_at REAL
        )
        """)
        
        conn.commit()
    logging.info("Database initialized successfully.")

# --- Tracked Chats ---
def add_tracked_chat(chat_id, title):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO tracked_chats (chat_id, title, added_at) VALUES (?, ?, ?)",
            (chat_id, title, time.time())
        )
        conn.commit()

def get_tracked_chats():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM tracked_chats")
        return [row[0] for row in cursor.fetchall()]

# --- User & Song Stats ---
def ensure_user(user_id, username, first_name):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, total_listen_seconds) VALUES (?, ?, ?, 0)",
            (user_id, username or "", first_name or "")
        )
        if username or first_name:
            cursor.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username or "", first_name or "", user_id)
            )
        conn.commit()

def add_play(user_id, username, first_name, song_title):
    ensure_user(user_id, username, first_name)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO song_stats (user_id, song_title, play_count) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, song_title) DO UPDATE SET play_count = play_count + 1",
            (user_id, song_title)
        )
        conn.commit()

def add_listen_time(user_id, seconds):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET total_listen_seconds = total_listen_seconds + ? WHERE user_id = ?",
            (int(seconds), user_id)
        )
        conn.commit()

def get_top_songs(user_id, limit=10):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT song_title, play_count FROM song_stats WHERE user_id = ? ORDER BY play_count DESC LIMIT ?",
            (user_id, limit)
        )
        return cursor.fetchall()

def get_user_profile(user_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT username, first_name, total_listen_seconds FROM users WHERE user_id = ?", (user_id,))
        user_row = cursor.fetchone()
        if not user_row:
            return None
        
        cursor.execute(
            "SELECT song_title, play_count FROM song_stats WHERE user_id = ? ORDER BY play_count DESC LIMIT 1",
            (user_id,)
        )
        fav_row = cursor.fetchone()
        
        favorite_song = fav_row["song_title"] if fav_row else "None"
        favorite_plays = fav_row["play_count"] if fav_row else 0
        
        return {
            "user_id": user_id,
            "username": user_row["username"] or "N/A",
            "first_name": user_row["first_name"] or "User",
            "total_listen_seconds": user_row["total_listen_seconds"],
            "favorite_song": favorite_song,
            "favorite_plays": favorite_plays
        }

# --- Group Chat Authorizations ---
def add_auth_user(chat_id, user_id, username):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO auth_users (chat_id, user_id, username) VALUES (?, ?, ?)",
            (chat_id, user_id, username or "")
        )
        conn.commit()

def remove_auth_user(chat_id, user_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM auth_users WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        conn.commit()

def is_auth_user(chat_id, user_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM auth_users WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        return cursor.fetchone() is not None

def get_auth_users(chat_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username FROM auth_users WHERE chat_id = ?", (chat_id,))
        return cursor.fetchall()

# --- Globally Approved Members ---
def add_approved_member(user_id, username):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO approved_members (user_id, username) VALUES (?, ?)",
            (user_id, username or "")
        )
        conn.commit()

def remove_approved_member(user_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM approved_members WHERE user_id = ?", (user_id,))
        conn.commit()

def is_approved_member(user_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM approved_members WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None

def get_approved_members():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username FROM approved_members")
        return cursor.fetchall()

# --- Group Chat Admin Cache ---
def get_cached_admins(chat_id):
    current_time = time.time()
    # Cache duration: 1 hour (3600 seconds)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM admin_cache WHERE chat_id = ? AND (? - cached_at) < 3600", (chat_id, current_time))
        rows = cursor.fetchall()
        if rows:
            return [row[0] for row in rows]
        return None

def set_cached_admins(chat_id, admin_ids):
    current_time = time.time()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admin_cache WHERE chat_id = ?", (chat_id,))
        for admin_id in admin_ids:
            cursor.execute(
                "INSERT INTO admin_cache (chat_id, user_id, cached_at) VALUES (?, ?, ?)",
                (chat_id, admin_id, current_time)
            )
        conn.commit()

def clear_admin_cache(chat_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM admin_cache WHERE chat_id = ?", (chat_id,))
        conn.commit()
