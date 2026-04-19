import re
import os
import hashlib
import json
import urllib.parse
import time
from pathlib import Path
from functools import lru_cache, wraps
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import secrets
import random

import mysql.connector
import pandas as pd
import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for, flash, send_file

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=7)

# ==================== Configuration ====================
BRAND_NAME = "Xstar Movies"
BRAND_MAIN = "Xstar"
BRAND_SUB = "Movies"

DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "Pushkar9312@",
    "database": "movie_recommendation",
}

OMDB_API_KEY = os.getenv("OMDB_API_KEY", "2ae492d0")

# Demo users
DEMO_USERS = {
    "admin": {"password": "admin12", "role": "admin", "name": "Administrator"},
    "john": {"password": "john123", "role": "premium", "name": "John Doe"},
    "sarah": {"password": "sarah123", "role": "user", "name": "Sarah Smith"},
    "mike": {"password": "mike123", "role": "user", "name": "Mike Johnson"},
}

# Sample movie trailers and streams (for demo)
SAMPLE_MEDIA = {
    "trailers": {
        "default": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ElephantsDream.mp4",
        "action": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
        "scifi": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/Sintel.mp4",
    },
    "movies": {
        1: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/BigBuckBunny.mp4",
        2: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ElephantsDream.mp4",
        3: "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
    }
}

# ==================== Database Functions ====================
def get_connection():
    """Create database connection."""
    try:
        return mysql.connector.connect(**DB_CONFIG)
    except mysql.connector.Error as e:
        print(f"Database error: {e}")
        return None

def query_rows(query: str, params: tuple = None) -> List[Dict]:
    """Execute SELECT query."""
    conn = get_connection()
    if not conn:
        return []
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(query, params or ())
        return cursor.fetchall()
    except Exception as e:
        print(f"Query error: {e}")
        return []
    finally:
        cursor.close()
        conn.close()

def execute_query(query: str, params: tuple = None) -> int:
    """Execute INSERT/UPDATE/DELETE query."""
    conn = get_connection()
    if not conn:
        return 0
    try:
        cursor = conn.cursor()
        cursor.execute(query, params or ())
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        print(f"Execute error: {e}")
        return 0
    finally:
        cursor.close()
        conn.close()

# ==================== Auth Functions ====================
def hash_password(password: str) -> str:
    """Hash password."""
    return hashlib.sha256(f"xstar_{password}_2024".encode()).hexdigest()

def verify_user(username: str, password: str) -> Optional[Dict]:
    """Verify user credentials."""
    if username in DEMO_USERS:
        if DEMO_USERS[username]["password"] == password:
            return {
                "user_id": list(DEMO_USERS.keys()).index(username) + 1,
                "username": username,
                "full_name": DEMO_USERS[username]["name"],
                "role": DEMO_USERS[username]["role"],
                "avatar": f"https://ui-avatars.com/api/?name={DEMO_USERS[username]['name'].replace(' ', '+')}&background=e50914&color=fff"
            }
    return None

def login_required(f):
    """Login required decorator."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please login to continue', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# Content sanitizer to avoid any sexual terms in rendered movie text
BLACKLISTED_TERMS = [
    "sex", "sexual", "erotic", "porn", "xxx", "nudity", "adult",
    "intimate", "seduction", "nude", "bare", "risque", "strip"
]

def sanitize_text(text: str) -> str:
    """Replace explicit or sexual terms with a safe placeholder."""
    if not text:
        return text
    pattern = re.compile(r'\b(' + '|'.join(map(re.escape, BLACKLISTED_TERMS)) + r')\b', re.IGNORECASE)
    return pattern.sub('Movie', text)

# ==================== Movie Functions ====================
CURATED_POSTERS = {
    "ransom": "https://upload.wikimedia.org/wikipedia/en/3/3f/Ransom_ver2.jpg",
    "cinderella": "https://upload.wikimedia.org/wikipedia/en/8/89/Cinderella_%281950_film%29.png",
}

def normalize_movie_title(title: str) -> str:
    """Normalize title for lookup in fallback poster map."""
    title_without_year = re.sub(r'\s*\(\d{4}\)', '', title or '').strip().lower()
    return re.sub(r'[^a-z0-9]+', ' ', title_without_year).strip()

def get_title_variants(movie_title: str) -> List[str]:
    """Build search variants for better OMDB title matching."""
    title_clean = re.sub(r'\s*\(\d{4}\)', '', movie_title or '').strip()
    variants: List[str] = []

    def add_variant(value: str):
        value = value.strip()
        if value and value not in variants:
            variants.append(value)

    add_variant(title_clean)

    # Convert titles like "Old Man and the Sea, The" to "The Old Man and the Sea"
    article_match = re.match(r'^(.*),\s*(The|An|A)$', title_clean, flags=re.IGNORECASE)
    if article_match:
        add_variant(f"{article_match.group(2)} {article_match.group(1)}")

    # Remove punctuation-heavy separators for fuzzy matching by OMDB
    add_variant(re.sub(r'[:\-_/]+', ' ', title_clean))
    return variants

def escape_svg_text(value: str) -> str:
    """Escape text for safe SVG embedding."""
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def infer_primary_genre(genres: Optional[str]) -> str:
    """Infer primary genre from pipe/comma-separated genre text."""
    if not genres:
        return "Drama"
    tokens = re.split(r"[|,]", genres)
    for token in tokens:
        token = token.strip()
        if token:
            return token
    return "Drama"

def get_genre_palette(primary_genre: str) -> Tuple[str, str, str]:
    """Return (color_a, color_b, accent) palette by genre."""
    palettes = {
        "Action": ("#1e3a8a", "#7f1d1d", "#f97316"),
        "Adventure": ("#065f46", "#1d4ed8", "#22d3ee"),
        "Animation": ("#7c3aed", "#2563eb", "#fbbf24"),
        "Comedy": ("#f59e0b", "#ef4444", "#fde047"),
        "Crime": ("#111827", "#374151", "#ef4444"),
        "Drama": ("#334155", "#7f1d1d", "#f87171"),
        "Fantasy": ("#4c1d95", "#0f766e", "#c084fc"),
        "Horror": ("#111111", "#7f1d1d", "#ef4444"),
        "Mystery": ("#1f2937", "#0f172a", "#a78bfa"),
        "Romance": ("#be185d", "#7c2d12", "#fb7185"),
        "Sci-Fi": ("#0f172a", "#1d4ed8", "#22d3ee"),
        "Thriller": ("#1f2937", "#7f1d1d", "#f43f5e"),
        "War": ("#3f3f46", "#1c1917", "#f59e0b"),
    }
    return palettes.get(primary_genre, ("#334155", "#7f1d1d", "#e5e7eb"))

def make_local_poster_data_uri(title: str, genres: Optional[str] = None) -> str:
    """Generate a self-contained cinematic SVG poster as a data URI."""
    safe_title = sanitize_text((title or "Movie")[:26])
    safe_title = escape_svg_text(safe_title)
    lines = []
    words = safe_title.split()
    current_line = ""
    for word in words:
        candidate = f"{current_line} {word}".strip()
        if len(candidate) <= 12:
            current_line = candidate
        else:
            if current_line:
                lines.append(current_line)
            current_line = word
    if current_line:
        lines.append(current_line)
    lines = lines[:3] if lines else ["Movie"]
    primary_genre = infer_primary_genre(genres)
    color_a, color_b, accent = get_genre_palette(primary_genre)
    genre_badge = escape_svg_text(primary_genre.upper())

    text_y = 228
    text_lines = "".join(
        f"<tspan x='50%' dy='{0 if i == 0 else 28}'>{line}</tspan>"
        for i, line in enumerate(lines)
    )
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='300' height='450' viewBox='0 0 300 450'>"
        "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        f"<stop offset='0%' stop-color='{color_a}'/>"
        f"<stop offset='100%' stop-color='{color_b}'/>"
        "</linearGradient>"
        "<radialGradient id='spot' cx='50%' cy='20%' r='70%'>"
        "<stop offset='0%' stop-color='rgba(255,255,255,0.26)'/>"
        "<stop offset='100%' stop-color='rgba(255,255,255,0)'/>"
        "</radialGradient>"
        "</defs>"
        "<rect width='300' height='450' fill='url(#g)'/>"
        "<rect width='300' height='450' fill='url(#spot)'/>"
        "<rect x='14' y='14' width='272' height='422' rx='10' fill='none' stroke='rgba(255,255,255,0.72)' stroke-width='2'/>"
        f"<rect x='28' y='28' width='104' height='26' rx='13' fill='rgba(0,0,0,0.34)' stroke='{accent}' stroke-width='1.2'/>"
        f"<text x='80' y='46' text-anchor='middle' fill='#f8fafc' font-family='Arial, sans-serif' font-size='11' font-weight='700'>{genre_badge}</text>"
        "<circle cx='250' cy='56' r='18' fill='rgba(255,255,255,0.18)'/>"
        "<path d='M40 360 C95 332, 205 332, 260 360 L260 450 L40 450 Z' fill='rgba(0,0,0,0.28)'/>"
        f"<text x='50%' y='{text_y}' text-anchor='middle' fill='#f2f2f2' font-family='Arial, sans-serif' font-size='24' font-weight='700'>"
        f"{text_lines}</text>"
        "<text x='50%' y='402' text-anchor='middle' fill='rgba(255,255,255,0.96)' font-family='Arial, sans-serif' font-size='16' font-weight='700' letter-spacing='1.2'>XSTAR MOVIES</text>"
        "</svg>"
    )
    return f"data:image/svg+xml;utf8,{urllib.parse.quote(svg)}"

def get_local_poster_file_url(title: str, genres: Optional[str] = None) -> str:
    """Create a local SVG poster file and return its static URL."""
    safe_title = sanitize_text((title or "Movie").strip())
    slug = re.sub(r'[^a-z0-9]+', '-', safe_title.lower()).strip('-') or "movie"
    posters_dir = Path(app.root_path) / "static" / "posters"
    posters_dir.mkdir(parents=True, exist_ok=True)
    poster_path = posters_dir / f"{slug}.svg"

    # Always rewrite so poster style updates are immediately visible.
    uri = make_local_poster_data_uri(safe_title, genres)
    svg_payload = urllib.parse.unquote(uri.split(",", 1)[1])
    poster_path.write_text(svg_payload, encoding="utf-8")

    return f"/static/posters/{slug}.svg"

def cache_remote_poster_url(title: str, poster_url: str, genres: Optional[str] = None) -> str:
    """Cache remote poster locally and return static path."""
    if not poster_url or poster_url.startswith("/static/"):
        return poster_url

    safe_title = sanitize_text((title or "Movie").strip())
    slug = re.sub(r'[^a-z0-9]+', '-', safe_title.lower()).strip('-') or "movie"
    cache_dir = Path(app.root_path) / "static" / "posters-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    candidate_ext = ".jpg"
    lower_url = poster_url.lower()
    for ext in [".jpg", ".jpeg", ".png", ".webp"]:
        if ext in lower_url:
            candidate_ext = ext
            break

    poster_path = cache_dir / f"{slug}{candidate_ext}"
    if poster_path.exists():
        return f"/static/posters-cache/{poster_path.name}"

    try:
        response = requests.get(poster_url, timeout=8)
        if response.status_code == 200 and response.content:
            poster_path.write_bytes(response.content)
            return f"/static/posters-cache/{poster_path.name}"
    except Exception as e:
        print(f"Poster cache error: {e}")

    return get_local_poster_file_url(title, genres)

def ensure_landing_posters(movies: List[Dict]) -> List[Dict]:
    """Use OMDB/IMDb poster first, then local fallback."""
    fixed_movies: List[Dict] = []
    cache_bust = int(time.time())
    for movie in movies:
        item = dict(movie)
        title_for_poster = item.get("title_clean") or item.get("title") or "Movie"
        genres_for_poster = item.get("genres") or item.get("genre")
        current_poster = item.get("poster", "")
        if current_poster:
            # Keep OMDB-derived poster (already cached to local static path in get_movie_details).
            item["poster"] = f"{current_poster}?v={cache_bust}" if "?" not in current_poster else current_poster
        else:
            # Fallback only when poster is missing.
            item["poster"] = f"{get_local_poster_file_url(title_for_poster, genres_for_poster)}?v={cache_bust}"
        fixed_movies.append(item)
    return fixed_movies

@lru_cache(maxsize=1)
def load_movies():
    """Load movies from database."""
    movies = query_rows("SELECT movieId, title, genres FROM movies LIMIT 1000")
    if not movies:
        # Generate sample movies if database is empty
        return generate_sample_movies()
    
    df = pd.DataFrame(movies)
    df['genres'] = df['genres'].fillna('')
    df['year'] = df['title'].apply(lambda x: re.search(r'\((\d{4})\)', str(x)).group(1) if re.search(r'\((\d{4})\)', str(x)) else '2024')
    df['title_clean'] = df['title'].apply(lambda x: sanitize_text(re.sub(r'\s*\(\d{4}\)', '', str(x))))
    df['title'] = df['title'].apply(sanitize_text)
    
    return df

def generate_sample_movies():
    """Generate sample movies for demo."""
    sample_movies = [
        {"movieId": 1, "title": "The Dark Knight (2008)", "genres": "Action|Crime|Drama"},
        {"movieId": 2, "title": "Inception (2010)", "genres": "Action|Adventure|Sci-Fi"},
        {"movieId": 3, "title": "Interstellar (2014)", "genres": "Adventure|Drama|Sci-Fi"},
        {"movieId": 4, "title": "The Matrix (1999)", "genres": "Action|Sci-Fi"},
        {"movieId": 5, "title": "Pulp Fiction (1994)", "genres": "Crime|Drama"},
        {"movieId": 6, "title": "The Shawshank Redemption (1994)", "genres": "Drama"},
        {"movieId": 7, "title": "The Godfather (1972)", "genres": "Crime|Drama"},
        {"movieId": 8, "title": "Forrest Gump (1994)", "genres": "Drama|Romance"},
        {"movieId": 9, "title": "Fight Club (1999)", "genres": "Drama"},
        {"movieId": 10, "title": "Goodfellas (1990)", "genres": "Biography|Crime|Drama"},
        {"movieId": 11, "title": "The Silence of the Lambs (1991)", "genres": "Crime|Drama|Thriller"},
        {"movieId": 12, "title": "Saving Private Ryan (1998)", "genres": "Drama|War"},
        {"movieId": 13, "title": "Gladiator (2000)", "genres": "Action|Adventure|Drama"},
        {"movieId": 14, "title": "The Departed (2006)", "genres": "Crime|Drama|Thriller"},
        {"movieId": 15, "title": "The Prestige (2006)", "genres": "Drama|Mystery|Sci-Fi"},
        {"movieId": 16, "title": "Django Unchained (2012)", "genres": "Drama|Western"},
        {"movieId": 17, "title": "Inglourious Basterds (2009)", "genres": "Adventure|Drama|War"},
        {"movieId": 18, "title": "Se7en (1995)", "genres": "Crime|Drama|Mystery"},
        {"movieId": 19, "title": "The Green Mile (1999)", "genres": "Crime|Drama|Fantasy"},
        {"movieId": 20, "title": "Léon: The Professional (1994)", "genres": "Action|Crime|Drama"},
        {"movieId": 21, "title": "Whiplash (2014)", "genres": "Drama|Music"},
        {"movieId": 22, "title": "Joker (2019)", "genres": "Crime|Drama|Thriller"},
        {"movieId": 23, "title": "Parasite (2019)", "genres": "Drama|Thriller"},
        {"movieId": 24, "title": "1917 (2019)", "genres": "Drama|War"},
        {"movieId": 25, "title": "Dune (2021)", "genres": "Action|Adventure|Drama|Sci-Fi"},
        {"movieId": 26, "title": "Spider-Man: No Way Home (2021)", "genres": "Action|Adventure|Fantasy"},
        {"movieId": 27, "title": "Top Gun: Maverick (2022)", "genres": "Action|Drama"},
        {"movieId": 28, "title": "Everything Everywhere All at Once (2022)", "genres": "Action|Adventure|Comedy"},
        {"movieId": 29, "title": "Oppenheimer (2023)", "genres": "Biography|Drama|History"},
        {"movieId": 30, "title": "Barbie (2023)", "genres": "Adventure|Comedy|Fantasy"},
    ]
    
    df = pd.DataFrame(sample_movies)
    df['year'] = df['title'].apply(lambda x: re.search(r'\((\d{4})\)', str(x)).group(1) if re.search(r'\((\d{4})\)', str(x)) else '2024')
    df['title_clean'] = df['title'].apply(lambda x: sanitize_text(re.sub(r'\s*\(\d{4}\)', '', str(x))))
    df['title'] = df['title'].apply(sanitize_text)
    
    return df

@lru_cache(maxsize=512)
def get_movie_details(movie_title: str, movie_genres: Optional[str] = None) -> Dict:
    """Get movie details from OMDB API (IMDb poster support)."""
    title_clean = re.sub(r'\s*\(\d{4}\)', '', movie_title).strip()
    title_variants = get_title_variants(movie_title)
    normalized_title = normalize_movie_title(movie_title)
    curated_poster = CURATED_POSTERS.get(normalized_title)
    year_match = re.search(r'\((\d{4})\)', movie_title)
    year = year_match.group(1) if year_match else ''

    def fetch_omdb(params: Dict) -> Optional[Dict]:
        try:
            response = requests.get("https://www.omdbapi.com/", params=params, timeout=6)
            data = response.json()
            if data.get("Response") == "True":
                return data
        except Exception as e:
            print(f"OMDB request error: {e}")
        return None

    data = None
    for candidate_title in title_variants:
        data = fetch_omdb({"apikey": OMDB_API_KEY, "t": candidate_title, "type": "movie", "y": year})
        if data:
            break

    if not data:
        for candidate_title in title_variants:
            data = fetch_omdb({"apikey": OMDB_API_KEY, "t": candidate_title, "type": "movie"})
            if data:
                break

    if not data or data.get("Poster") in [None, "N/A", ""]:
        for candidate_title in title_variants:
            search_data = fetch_omdb({"apikey": OMDB_API_KEY, "s": candidate_title, "type": "movie"})
            if search_data and search_data.get("Search"):
                first_hit = search_data["Search"][0]
                imdb_id = first_hit.get("imdbID")
                if imdb_id:
                    data = fetch_omdb({"apikey": OMDB_API_KEY, "i": imdb_id, "plot": "short", "type": "movie"})
                    if data:
                        break

    if data and data.get("Response") == "True":
        poster_url = data.get("Poster", "")
        if poster_url in [None, "N/A", ""]:
            poster_url = curated_poster or get_local_poster_file_url(title_clean, movie_genres)
        poster_url = cache_remote_poster_url(title_clean, poster_url, movie_genres)

        return {
            "poster": poster_url,
            "plot": sanitize_text(data.get("Plot", "An exciting movie that will keep you on the edge of your seat.")),
            "imdbRating": data.get("imdbRating", "N/A"),
            "runtime": sanitize_text(data.get("Runtime", "N/A")),
            "director": sanitize_text(data.get("Director", "N/A")),
            "actors": sanitize_text(data.get("Actors", "N/A")),
            "cast": sanitize_text(data.get("Actors", "N/A")),
            "genre": sanitize_text(data.get("Genre", "N/A")),
            "imdbID": data.get("imdbID", ""),
            "released": sanitize_text(data.get("Released", "N/A")),
            "language": sanitize_text(data.get("Language", "N/A")),
            "country": sanitize_text(data.get("Country", "N/A")),
            "awards": sanitize_text(data.get("Awards", "N/A")),
        }

    return {
        "poster": cache_remote_poster_url(title_clean, curated_poster, movie_genres) if curated_poster else get_local_poster_file_url(title_clean, movie_genres),
        "plot": sanitize_text("An exciting movie that will keep you on the edge of your seat."),
        "imdbRating": f"{random.uniform(7.0, 9.5):.1f}",
        "runtime": sanitize_text(f"{random.randint(90, 180)} min"),
        "director": sanitize_text("Acclaimed Director"),
        "actors": sanitize_text("Star Actor, Leading Actress, Supporting Actor"),
        "cast": sanitize_text("Star Actor, Leading Actress, Supporting Actor"),
        "genre": sanitize_text("Drama, Action"),
        "imdbID": "",
        "released": sanitize_text("N/A"),
        "language": sanitize_text("N/A"),
        "country": sanitize_text("N/A"),
        "awards": sanitize_text("N/A"),
    }

def get_recommendations(movie_id: int, limit: int = 12) -> List[Dict]:
    """Get movie recommendations based on genres."""
    df = load_movies()
    
    # Find the movie
    movie = df[df['movieId'] == movie_id]
    if movie.empty:
        return []
    
    movie_genres = set(movie.iloc[0]['genres'].split('|'))
    
    # Find similar movies based on genre overlap
    recommendations = []
    for _, row in df.iterrows():
        if row['movieId'] == movie_id:
            continue
        
        row_genres = set(row['genres'].split('|'))
        overlap = len(movie_genres & row_genres)
        
        if overlap > 0:
            recommendations.append({
                "movieId": row['movieId'],
                "title": row['title'],
                "genres": row['genres'],
                "year": row['year'],
                "title_clean": row['title_clean'],
                "score": overlap
            })
    
    # Sort by genre overlap
    recommendations.sort(key=lambda x: x['score'], reverse=True)
    
    # Add details to top recommendations
    result = []
    for rec in recommendations[:limit]:
        details = get_movie_details(rec['title'], rec.get('genres'))
        result.append({
            **rec,
            **details,
            "stream_url": SAMPLE_MEDIA["movies"].get(rec['movieId'], SAMPLE_MEDIA["trailers"]["default"])
        })
    
    return result

def get_trending_movies(limit: int = 12) -> List[Dict]:
    """Get trending movies."""
    df = load_movies()
    trending = df.sample(min(limit, len(df)))
    
    result = []
    for _, row in trending.iterrows():
        details = get_movie_details(row['title'], row.get('genres'))
        result.append({
            "movieId": row['movieId'],
            "title": row['title'],
            "title_clean": row['title_clean'],
            "genres": row['genres'],
            "year": row['year'],
            **details,
            "stream_url": SAMPLE_MEDIA["movies"].get(row['movieId'], SAMPLE_MEDIA["trailers"]["default"])
        })
    
    return result

def get_top_rated(limit: int = 12) -> List[Dict]:
    """Get top rated movies."""
    df = load_movies()
    top = df.head(limit)
    
    result = []
    for _, row in top.iterrows():
        details = get_movie_details(row['title'], row.get('genres'))
        details['imdbRating'] = f"{random.uniform(8.0, 9.5):.1f}"  # Higher ratings for top rated
        result.append({
            "movieId": row['movieId'],
            "title": row['title'],
            "title_clean": row['title_clean'],
            "genres": row['genres'],
            "year": row['year'],
            **details,
            "stream_url": SAMPLE_MEDIA["movies"].get(row['movieId'], SAMPLE_MEDIA["trailers"]["default"])
        })
    
    return result

def get_genre_movies(genre: str, limit: int = 12) -> List[Dict]:
    """Get movies by genre."""
    df = load_movies()
    genre_movies = df[df['genres'].str.contains(genre, case=False, na=False)]
    
    if genre_movies.empty:
        genre_movies = df
    
    sample_movies = genre_movies.head(limit)
    
    result = []
    for _, row in sample_movies.iterrows():
        details = get_movie_details(row['title'], row.get('genres'))
        result.append({
            "movieId": row['movieId'],
            "title": row['title'],
            "title_clean": row['title_clean'],
            "genres": row['genres'],
            "year": row['year'],
            **details,
            "stream_url": SAMPLE_MEDIA["movies"].get(row['movieId'], SAMPLE_MEDIA["trailers"]["default"])
        })
    
    return result

# ==================== Routes ====================
@app.route('/')
def index():
    """Landing page."""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    featured = ensure_landing_posters(get_trending_movies(1))
    featured_movie = featured[0] if featured else {
        "movieId": 2,
        "title": "Inception",
        "poster": "https://images.unsplash.com/photo-1542204165-4d15ef8358f8?auto=format&fit=crop&w=1200&q=80",
        "plot": "A brilliant mind-bending thriller from Christopher Nolan.",
        "imdbRating": "8.8",
        "runtime": "148 min",
        "year": "2010",
    }
    showcase_movies = ensure_landing_posters(get_trending_movies(4))

    return render_template('landing.html',
                         brand_name=BRAND_NAME,
                         brand_main=BRAND_MAIN,
                         brand_sub=BRAND_SUB,
                         featured_movie=featured_movie,
                         showcase_movies=showcase_movies)

@app.route('/landing')
def landing():
    """Landing page."""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))

    featured = ensure_landing_posters(get_trending_movies(1))
    featured_movie = featured[0] if featured else {
        "movieId": 2,
        "title": "Inception",
        "poster": "https://images.unsplash.com/photo-1542204165-4d15ef8358f8?auto=format&fit=crop&w=1200&q=80",
        "plot": "A brilliant mind-bending thriller from Christopher Nolan.",
        "imdbRating": "8.8",
        "runtime": "148 min",
        "year": "2010",
    }
    showcase_movies = ensure_landing_posters(get_trending_movies(4))

    return render_template('landing.html',
                         brand_name=BRAND_NAME,
                         brand_main=BRAND_MAIN,
                         brand_sub=BRAND_SUB,
                         featured_movie=featured_movie,
                         showcase_movies=showcase_movies)

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page."""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        remember = request.form.get('remember') == 'on'
        
        user = verify_user(username, password)
        if user:
            session.permanent = remember
            session['user_id'] = user['user_id']
            session['username'] = user['username']
            session['full_name'] = user['full_name']
            session['role'] = user['role']
            session['avatar'] = user['avatar']
            
            flash(f'Welcome back, {user["full_name"]}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            error = 'Invalid username or password'
    
    return render_template('login.html', 
                         error=error,
                         brand_name=BRAND_NAME,
                         brand_main=BRAND_MAIN,
                         brand_sub=BRAND_SUB,
                         demo_users=DEMO_USERS)

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    """Signup page."""
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    
    error = None
    
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        email = request.form.get('email', '').strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()
        terms = request.form.get('terms')
        
        # Validation
        if not all([full_name, email, username, password]):
            error = 'All fields are required'
        elif len(username) < 3:
            error = 'Username must be at least 3 characters'
        elif len(password) < 8:
            error = 'Password must be at least 8 characters'
        elif password != confirm_password:
            error = 'Passwords do not match'
        elif not terms:
            error = 'You must accept the terms and conditions'
        elif username in DEMO_USERS:
            error = 'Username already exists'
        else:
            # Add new user
            DEMO_USERS[username] = {
                "password": password,
                "role": "user",
                "name": full_name
            }
            flash(f'Welcome {full_name}! Your account has been created. Please sign in.', 'success')
            return redirect(url_for('login'))
    
    return render_template('signup.html',
                         error=error,
                         request=request,
                         brand_name=BRAND_NAME,
                         brand_main=BRAND_MAIN,
                         brand_sub=BRAND_SUB)

@app.route('/dashboard')
@login_required
def dashboard():
    """Main dashboard with movie recommendations."""
    # Get movies for display
    trending = get_trending_movies(12)
    top_rated = get_top_rated(12)
    action_movies = get_genre_movies('Action', 12)
    scifi_movies = get_genre_movies('Sci-Fi', 12)
    drama_movies = get_genre_movies('Drama', 12)
    comedy_movies = get_genre_movies('Comedy', 12)
    
    # Featured movie (first trending)
    featured = trending[0] if trending else None
    
    # Personalized recommendations based on a random movie the user might like
    if featured:
        recommendations = get_recommendations(featured['movieId'], 12)
    else:
        recommendations = top_rated
    
    # User's watchlist (mock data)
    watchlist = trending[:4] if trending else []
    
    return render_template('dashboard.html',
                         username=session.get('username'),
                         full_name=session.get('full_name'),
                         role=session.get('role'),
                         avatar=session.get('avatar'),
                         featured=featured,
                         trending=trending,
                         top_rated=top_rated,
                         recommendations=recommendations,
                         action_movies=action_movies,
                         scifi_movies=scifi_movies,
                         drama_movies=drama_movies,
                         comedy_movies=comedy_movies,
                         watchlist=watchlist,
                         brand_name=BRAND_NAME,
                         brand_main=BRAND_MAIN,
                         brand_sub=BRAND_SUB)

@app.route('/watch/<int:movie_id>')
@login_required
def watch_movie(movie_id):
    """Watch movie page."""
    df = load_movies()
    movie = df[df['movieId'] == movie_id]
    
    if movie.empty:
        flash('Movie not found', 'error')
        return redirect(url_for('dashboard'))
    
    movie_data = movie.iloc[0].to_dict()
    details = get_movie_details(movie_data['title'], movie_data.get('genres'))
    stream_url = SAMPLE_MEDIA["movies"].get(movie_id, SAMPLE_MEDIA["trailers"]["default"])
    
    # Get similar movies
    similar = get_recommendations(movie_id, 12)
    
    return render_template('watch.html',
                         movie=movie_data,
                         details=details,
                         stream_url=stream_url,
                         similar=similar,
                         username=session.get('username'),
                         avatar=session.get('avatar'),
                         brand_name=BRAND_NAME)

@app.route('/movie/<int:movie_id>')
@login_required
def movie_details(movie_id):
    """Movie details page."""
    df = load_movies()
    movie = df[df['movieId'] == movie_id]
    
    if movie.empty:
        flash('Movie not found', 'error')
        return redirect(url_for('dashboard'))
    
    movie_data = movie.iloc[0].to_dict()
    details = get_movie_details(movie_data['title'], movie_data.get('genres'))
    
    # Get recommendations
    similar = get_recommendations(movie_id, 12)
    
    return render_template('movie_details.html',
                         movie=movie_data,
                         details=details,
                         similar=similar,
                         username=session.get('username'),
                         avatar=session.get('avatar'),
                         brand_name=BRAND_NAME)

@app.route('/search')
@login_required
def search():
    """Search movies."""
    query = request.args.get('q', '').strip()
    
    if not query:
        return render_template('search.html',
                             query='',
                             results=[],
                             username=session.get('username'),
                             avatar=session.get('avatar'),
                             brand_name=BRAND_NAME)
    
    df = load_movies()
    results = df[df['title_clean'].str.contains(query, case=False, na=False)]
    
    search_results = []
    for _, row in results.head(20).iterrows():
        details = get_movie_details(row['title'], row.get('genres'))
        search_results.append({
            "movieId": row['movieId'],
            "title": row['title'],
            "title_clean": row['title_clean'],
            "genres": row['genres'],
            "year": row['year'],
            **details
        })
    
    return render_template('search.html',
                         query=query,
                         results=search_results,
                         username=session.get('username'),
                         avatar=session.get('avatar'),
                         brand_name=BRAND_NAME)

@app.route('/category/<genre>')
@login_required
def category(genre):
    """Category/genre page."""
    movies = get_genre_movies(genre, 50)
    
    return render_template('category.html',
                         genre=genre,
                         movies=movies,
                         username=session.get('username'),
                         avatar=session.get('avatar'),
                         brand_name=BRAND_NAME)

@app.route('/my-list')
@login_required
def my_list():
    """User's watchlist."""
    # Mock watchlist data
    watchlist = get_trending_movies(8)
    
    return render_template('my_list.html',
                         watchlist=watchlist,
                         username=session.get('username'),
                         avatar=session.get('avatar'),
                         brand_name=BRAND_NAME)

@app.route('/logout')
def logout():
    """Logout user."""
    session.clear()
    flash('You have been logged out', 'info')
    return redirect(url_for('login'))

# ==================== API Routes ====================
@app.route('/api/trending')
@login_required
def api_trending():
    """API: Get trending movies."""
    return jsonify(get_trending_movies(12))

@app.route('/api/recommendations/<int:movie_id>')
@login_required
def api_recommendations(movie_id):
    """API: Get movie recommendations."""
    return jsonify(get_recommendations(movie_id, 12))

@app.route('/api/recommend')
@login_required
def api_recommend():
    """API: Recommend movies with selected movie details."""
    movie_id = request.args.get('movie_id', type=int)
    if not movie_id:
        return jsonify({"error": "movie_id query parameter is required"}), 400

    df = load_movies()
    movie = df[df['movieId'] == movie_id]
    selected = {}
    if not movie.empty:
        movie_data = movie.iloc[0].to_dict()
        selected = {
            "movieId": movie_data['movieId'],
            "title": movie_data['title'],
            "title_clean": movie_data['title_clean'],
            "year": movie_data['year'],
            "genres": movie_data['genres'],
            "genresLabel": movie_data['genres'].replace('|', ', '),
            **get_movie_details(movie_data['title'], movie_data.get('genres'))
        }

    return jsonify({
        "selected": selected,
        "recommendations": get_recommendations(movie_id, 12)
    })

@app.route('/api/movie/<int:movie_id>')
@login_required
def api_movie(movie_id):
    """API: Get movie details."""
    df = load_movies()
    movie = df[df['movieId'] == movie_id]
    if movie.empty:
        return jsonify({"error": "Movie not found"}), 404

    movie_data = movie.iloc[0].to_dict()
    details = get_movie_details(movie_data['title'], movie_data.get('genres'))
    return jsonify({
        "movieId": movie_data['movieId'],
        "title": movie_data['title'],
        "title_clean": movie_data['title_clean'],
        "year": movie_data['year'],
        "genres": movie_data['genres'],
        "genresLabel": movie_data['genres'].replace('|', ', '),
        "poster": details['poster'],
        "plot": details['plot'],
        "imdbRating": details['imdbRating'],
        "runtime": details['runtime'],
        "director": details['director'],
        "actors": details['actors'],
        "cast": details.get('cast', details['actors']),
        "genre": details['genre'],
        "imdbId": details.get('imdbID', ''),
        "released": details.get('released', 'N/A'),
        "language": details.get('language', 'N/A'),
        "country": details.get('country', 'N/A'),
        "awards": details.get('awards', 'N/A'),
    })

@app.route('/api/search')
@login_required
def api_search():
    """API: Search movies."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    
    df = load_movies()
    results = df[df['title_clean'].str.contains(query, case=False, na=False)]
    
    return jsonify([{
        "movieId": row['movieId'],
        "title": row['title'],
        "releaseYear": row['year'],
        "genresLabel": row['genres'].replace('|', ', ')
    } for _, row in results.head(10).iterrows()])

@app.route('/api/rate', methods=['POST'])
@login_required
def api_rate():
    """API: Rate a movie."""
    data = request.json
    movie_id = data.get('movie_id')
    rating = data.get('rating')
    
    # In production, save to database
    return jsonify({"success": True, "message": "Rating saved"})

# ==================== Error Handlers ====================
@app.errorhandler(404)
def not_found(e):
    return render_template('404.html', brand_name=BRAND_NAME), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('500.html', brand_name=BRAND_NAME), 500

# ==================== Run Application ====================
if __name__ == '__main__':
    print("=" * 50)
    print(f"🎬 {BRAND_NAME} Server Starting...")
    print("=" * 50)
    print("\n📺 Demo Accounts:")
    for username, info in DEMO_USERS.items():
        print(f"   • {username} / {info['password']} ({info['role']})")
    print("\n🌐 Access at: http://localhost:5000")
    print("=" * 50)
   port = int(os.environ.get("PORT", 5000))  # Convert to integer
app.run(host="0.0.0.0", port=port)
