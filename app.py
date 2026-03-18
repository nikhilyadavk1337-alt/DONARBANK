import os
import math
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, g, send_from_directory, make_response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import jwt
from flask_socketio import SocketIO, emit, join_room
import traceback
import psycopg2
import psycopg2.extras

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secret-lifedrop-key')
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Supabase Postgres URL (Set this in Render Environment Variables)
DB_URL = os.environ.get('DATABASE_URL', 'postgresql://postgres:your_password@db.supabase.co:5432/postgres')

CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

@app.errorhandler(Exception)
def handle_exception(e):
    traceback.print_exc()
    return jsonify({"success": False, "message": "Server processing error. Please try again."}), 500

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None: db.close()

def init_db():
    db = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cursor = db.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT DEFAULT 'user', is_banned BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS donors (id SERIAL PRIMARY KEY, user_id INTEGER UNIQUE, name TEXT NOT NULL, blood_group TEXT NOT NULL, lat REAL, lon REAL, available BOOLEAN DEFAULT TRUE, rating REAL DEFAULT 5.0, total_donations INTEGER DEFAULT 0, last_donation_date TIMESTAMP, is_verified BOOLEAN DEFAULT FALSE, document_path TEXT, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS hospitals (id SERIAL PRIMARY KEY, user_id INTEGER UNIQUE, name TEXT NOT NULL, lat REAL, lon REAL, stock_ap REAL DEFAULT 0, stock_an REAL DEFAULT 0, stock_bp REAL DEFAULT 0, stock_bn REAL DEFAULT 0, stock_op REAL DEFAULT 0, stock_on REAL DEFAULT 0, stock_abp REAL DEFAULT 0, stock_abn REAL DEFAULT 0, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS requests (id SERIAL PRIMARY KEY, user_id INTEGER, blood_group TEXT NOT NULL, lat REAL, lon REAL, status TEXT DEFAULT 'pending', is_priority BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS donations (id SERIAL PRIMARY KEY, donor_id INTEGER, request_id INTEGER, status TEXT DEFAULT 'accepted', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(donor_id) REFERENCES donors(id) ON DELETE CASCADE, FOREIGN KEY(request_id) REFERENCES requests(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS reviews (id SERIAL PRIMARY KEY, donor_id INTEGER, rating INTEGER, comment TEXT, FOREIGN KEY(donor_id) REFERENCES donors(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS enquiries (id SERIAL PRIMARY KEY, name TEXT, email TEXT, message TEXT, status TEXT DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS payments (id SERIAL PRIMARY KEY, user_id INTEGER, request_id INTEGER, amount REAL, screenshot_path TEXT, status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE INDEX IF NOT EXISTS idx_blood_group ON donors(blood_group);
        CREATE INDEX IF NOT EXISTS idx_request_location ON requests(lat, lon);
    ''')
    
    cursor.execute("SELECT * FROM users WHERE email='nikhiladmin'")
    if not cursor.fetchone():
        cursor.execute("INSERT INTO users (email, password, role) VALUES (%s, %s, %s)", ('nikhiladmin', generate_password_hash('nikhil9936'), 'admin'))
    db.commit()
    db.close()

# Initialize DB (only if running locally or first time on Render)
try:
    init_db()
except Exception as e:
    print("DB Init Error:", e)

@app.route('/')
def serve_index(): return send_from_directory('.', 'index.html')

@app.route('/uploads/<filename>')
def uploaded_file(filename): return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/manifest.json')
def manifest():
    return jsonify({"name": "LifeDrop Network", "short_name": "LifeDrop", "start_url": "/", "display": "standalone", "background_color": "#050505", "theme_color": "#e11d48", "icons": [{"src": "https://cdn-icons-png.flaticon.com/512/3063/3063205.png", "sizes": "512x512", "type": "image/png"}]})

@app.route('/sw.js')
def sw():
    js = "self.addEventListener('install', e => e.waitUntil(caches.open('ld-v1').then(c => c.addAll(['/'])))); self.addEventListener('fetch', e => e.respondWith(caches.match(e.request).then(r => r || fetch(e.request))));"
    res = make_response(js)
    res.headers['Content-Type'] = 'application/javascript'
    return res

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token: return jsonify({'success': False, 'message': 'Authentication required!'}), 401
        try:
            token = token.split(" ")[1]
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            current_user = get_db().cursor().execute("SELECT * FROM users WHERE id=%s", (data['user_id'],)).fetchone()
            if not current_user or current_user['is_banned']: raise Exception()
        except: return jsonify({'success': False, 'message': 'Session expired. Please login again.'}), 401
        return f(current_user, *args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(current_user, *args, **kwargs):
        if current_user['role'] != 'admin': return jsonify({'success': False, 'message': 'Power Admin access required!'}), 403
        return f(current_user, *args, **kwargs)
    return decorated

def calculate_distance(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2): return float('inf')
    try:
        R, dlat, dlon = 6371, math.radians(float(lat2) - float(lat1)), math.radians(float(lon2) - float(lon1))
        a = math.sin(dlat/2)**2 + math.cos(math.radians(float(lat1))) * math.cos(math.radians(float(lat2))) * math.sin(dlon/2)**2
        return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))
    except: return float('inf')

@socketio.on('join')
def on_join(data):
    if data.get('user_id'): join_room(f"user_{data['user_id']}")

@app.route('/api/auth', methods=['POST'])
def auth():
    data = request.json
    action, email, password = data.get('action'), data.get('email'), data.get('password')
    db = get_db()
    c = db.cursor()
    
    if action == 'register':
        c.execute("SELECT id FROM users WHERE email=%s", (email,))
        if c.fetchone(): return jsonify({"success": False, "message": "Email already exists!"})
        c.execute("INSERT INTO users (email, password, role) VALUES (%s, %s, %s)", (email, generate_password_hash(password), data.get('role', 'user')))
        db.commit()
        return jsonify({"success": True, "message": "Account created! Welcome to LifeDrop."})
    
    elif action == 'login':
        c.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = c.fetchone()
        if user and check_password_hash(user['password'], password):
            if user['is_banned']: return jsonify({"success": False, "message": "This account has been banned."}), 403
            token = jwt.encode({'user_id': user['id'], 'exp': datetime.utcnow() + timedelta(days=7)}, app.config['SECRET_KEY'], algorithm="HS256")
            return jsonify({"success": True, "token": token, "role": user['role'], "id": user['id'], "email": user['email']})
        return jsonify({"success": False, "message": "Invalid email or password!"})

@app.route('/api/profile/update', methods=['POST'])
@token_required
def update_profile(current_user):
    db = get_db()
    c = db.cursor()
    
    if current_user['role'] == 'user':
        name, bg, lat, lon, avail = request.form.get('name'), request.form.get('blood_group'), request.form.get('lat'), request.form.get('lon'), request.form.get('available') == 'true'
        doc_path = None
        if 'document' in request.files:
            file = request.files['document']
            doc_path = secure_filename(f"doc_{current_user['id']}_{file.filename}")
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], doc_path))

        c.execute("SELECT id FROM donors WHERE user_id=%s", (current_user['id'],))
        existing = c.fetchone()
        if existing:
            c.execute("UPDATE donors SET name=%s, blood_group=%s, lat=%s, lon=%s, available=COALESCE(%s, available), document_path=COALESCE(%s, document_path) WHERE user_id=%s", (name, bg, lat, lon, avail, doc_path, current_user['id']))
        else:
            c.execute("INSERT INTO donors (user_id, name, blood_group, lat, lon, available, document_path) VALUES (%s, %s, %s, %s, %s, %s, %s)", (current_user['id'], name, bg, lat, lon, avail, doc_path))
            
    elif current_user['role'] == 'hospital':
        name, lat, lon = request.form.get('name'), request.form.get('lat'), request.form.get('lon')
        c.execute("SELECT id FROM hospitals WHERE user_id=%s", (current_user['id'],))
        existing = c.fetchone()
        if existing: c.execute("UPDATE hospitals SET name=%s, lat=%s, lon=%s WHERE user_id=%s", (name, lat, lon, current_user['id']))
        else: c.execute("INSERT INTO hospitals (user_id, name, lat, lon) VALUES (%s, %s, %s, %s)", (current_user['id'], name, lat, lon))
        
    db.commit()
    return jsonify({"success": True, "message": "Profile synchronized successfully!"})

@app.route('/api/hospital/stock', methods=['POST'])
@token_required
def update_stock(current_user):
    if current_user['role'] != 'hospital': return jsonify({"success":False})
    data = request.json
    db = get_db()
    db.cursor().execute("UPDATE hospitals SET stock_ap=%s, stock_an=%s, stock_bp=%s, stock_bn=%s, stock_op=%s, stock_on=%s, stock_abp=%s, stock_abn=%s WHERE user_id=%s",
                        (data.get('A+'), data.get('A-'), data.get('B+'), data.get('B-'), data.get('O+'), data.get('O-'), data.get('AB+'), data.get('AB-'), current_user['id']))
    db.commit()
    return jsonify({"success": True, "message": "Global inventory updated!"})

@app.route('/api/find_donors', methods=['POST'])
@token_required
def find_donors(current_user):
    data = request.json
    lat, lon, bg = data.get('lat'), data.get('lon'), data.get('blood_group')
    db = get_db()
    
    c = db.cursor()
    c.execute("SELECT * FROM donors WHERE blood_group=%s AND available=TRUE AND user_id!=%s", (bg, current_user['id']))
    donors = c.fetchall()
    
    # Secure string replacement for Postgres column names
    col_name = f"stock_{bg.replace('+','p').replace('-','n')}"
    c.execute(f"SELECT * FROM hospitals WHERE {col_name} > 0")
    hospitals = c.fetchall()
    
    results = []
    for d in donors:
        d_dict = dict(d)
        d_dict['type'] = 'donor'
        d_dict['distance_km'] = round(calculate_distance(lat, lon, d['lat'], d['lon']), 2)
        results.append(d_dict)
        
    for h in hospitals:
        h_dict = dict(h)
        h_dict['type'] = 'hospital'
        h_dict['distance_km'] = round(calculate_distance(lat, lon, h['lat'], h['lon']), 2)
        h_dict['rating'] = 5.0 
        results.append(h_dict)
        
    results.sort(key=lambda x: x['distance_km'])
    return jsonify({"success": True, "data": results})

@app.route('/api/create_request', methods=['POST'])
@token_required
def create_request(current_user):
    data = request.json
    db = get_db()
    c = db.cursor()
    c.execute("INSERT INTO requests (user_id, blood_group, lat, lon) VALUES (%s, %s, %s, %s) RETURNING id", (current_user['id'], data.get('blood_group'), data.get('lat'), data.get('lon')))
    req_id = c.fetchone()['id']
    db.commit()
    
    c.execute("SELECT * FROM donors WHERE blood_group=%s AND available=TRUE AND user_id!=%s", (data.get('blood_group'), current_user['id']))
    donors = c.fetchall()
    scored = []
    for d in donors:
        dist = calculate_distance(data.get('lat'), data.get('lon'), d['lat'], d['lon'])
        score = (1 / max(dist, 0.1)) * 50 + (d['rating'] * 30) + (20 if d['is_verified'] else 0)
        scored.append((score, dict(d)))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    top_3 = [d[1] for d in scored[:3]]

    for d in top_3:
        socketio.emit('auto_assign', {"request_id": req_id, "blood_group": data.get('blood_group'), "lat": data.get('lat'), "lon": data.get('lon')}, room=f"user_{d['user_id']}")

    return jsonify({"success": True, "message": "Emergency signal broadcasted. AI routing in progress.", "request_id": req_id})

@app.route('/api/accept_request', methods=['POST'])
@token_required
def accept_request(current_user):
    req_id = request.json.get('request_id')
    db = get_db()
    c = db.cursor()
    
    c.execute("SELECT * FROM donors WHERE user_id=%s", (current_user['id'],))
    donor = c.fetchone()
    if not donor: return jsonify({"success": False, "message": "Please setup your donor profile first."})
    
    if donor['last_donation_date']:
        # Ensure correct datetime parsing based on Postgres format
        if isinstance(donor['last_donation_date'], str):
            last_don = datetime.strptime(donor['last_donation_date'].split('.')[0], '%Y-%m-%d %H:%M:%S')
        else:
            last_don = donor['last_donation_date']
            
        days_since = (datetime.utcnow() - last_don).days
        if days_since < 90: return jsonify({"success": False, "message": f"Medical cooldown active. {90 - days_since} days remaining."})

    c.execute("INSERT INTO donations (donor_id, request_id) VALUES (%s, %s)", (donor['id'], req_id))
    c.execute("UPDATE requests SET status='accepted' WHERE id=%s", (req_id,))
    c.execute("UPDATE donors SET last_donation_date=CURRENT_TIMESTAMP, total_donations=total_donations+1 WHERE id=%s", (donor['id'],))
    db.commit()
    
    c.execute("SELECT user_id FROM requests WHERE id=%s", (req_id,))
    req = c.fetchone()
    socketio.emit('request_accepted', {"donor_name": donor['name'], "message": f"{donor['name']} is en route to help!"}, room=f"user_{req['user_id']}")
    return jsonify({"success": True, "message": "Request accepted. You are a hero."})

@app.route('/api/payment/priority', methods=['POST'])
@token_required
def boost_request(current_user):
    req_id = request.form.get('request_id')
    file = request.files.get('screenshot')
    if not file: return jsonify({"success": False, "message": "Payment proof screenshot is required."})
    
    filename = secure_filename(f"pay_{current_user['id']}_{file.filename}")
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    
    db = get_db()
    db.cursor().execute("INSERT INTO payments (user_id, request_id, amount, screenshot_path) VALUES (%s, %s, 20.0, %s)", (current_user['id'], req_id, filename))
    db.commit()
    return jsonify({"success": True, "message": "Proof uploaded. Boost will activate upon admin confirmation."})

@app.route('/api/enquiry', methods=['POST'])
def submit_enquiry():
    data = request.json
    db = get_db()
    db.cursor().execute("INSERT INTO enquiries (name, email, message) VALUES (%s, %s, %s)", (data.get('name'), data.get('email'), data.get('message')))
    db.commit()
    return jsonify({"success": True, "message": "Support ticket created. We will be in touch."})

@app.route('/api/demand_heatmap', methods=['GET'])
def get_heatmap():
    db = get_db()
    c = db.cursor()
    c.execute("SELECT ROUND(lat::numeric, 2) as lat, ROUND(lon::numeric, 2) as lon, COUNT(*) as weight FROM requests GROUP BY ROUND(lat::numeric, 2), ROUND(lon::numeric, 2)")
    data = c.fetchall()
    return jsonify({"success": True, "data": [dict(d) for d in data]})

@app.route('/api/dashboard', methods=['GET'])
@token_required
def get_dashboard(current_user):
    db = get_db()
    c = db.cursor()
    
    if current_user['role'] == 'user':
        c.execute("SELECT * FROM donors WHERE user_id=%s", (current_user['id'],))
        donor = c.fetchone()
        
        c.execute("SELECT * FROM requests WHERE user_id=%s ORDER BY id DESC", (current_user['id'],))
        requests_made = c.fetchall()
        
        donations = []
        if donor:
            c.execute("SELECT d.*, r.blood_group, r.created_at as req_date FROM donations d JOIN requests r ON d.request_id = r.id WHERE d.donor_id=%s ORDER BY d.id DESC", (donor['id'],))
            donations = c.fetchall()
            
        return jsonify({"success": True, "profile": dict(donor) if donor else None, "requests": [dict(r) for r in requests_made], "donations": [dict(d) for d in donations]})
    
    elif current_user['role'] == 'hospital':
        c.execute("SELECT * FROM hospitals WHERE user_id=%s", (current_user['id'],))
        hosp = c.fetchone()
        return jsonify({"success": True, "profile": dict(hosp) if hosp else None})
    
    return jsonify({"success": True, "message": "Admin operational."})

@app.route('/api/admin/data', methods=['GET'])
@token_required
@admin_required
def admin_data(current_user):
    c = get_db().cursor()
    
    c.execute("SELECT id, email, role, is_banned FROM users")
    users = c.fetchall()
    
    c.execute("SELECT id, name, is_verified, document_path FROM donors")
    donors = c.fetchall()
    
    c.execute("SELECT * FROM payments WHERE status='pending'")
    payments = c.fetchall()
    
    c.execute("SELECT * FROM enquiries ORDER BY id DESC")
    enquiries = c.fetchall()
    
    c.execute("SELECT COUNT(*) as cnt FROM requests")
    total_reqs = c.fetchone()['cnt']
    
    c.execute("SELECT COUNT(*) as cnt FROM donations")
    total_dons = c.fetchone()['cnt']
    
    return jsonify({
        "success": True,
        "users": [dict(u) for u in users],
        "donors": [dict(d) for d in donors],
        "payments": [dict(p) for p in payments],
        "enquiries": [dict(e) for e in enquiries],
        "stats": {
            "total_reqs": total_reqs,
            "total_dons": total_dons
        }
    })

@app.route('/api/admin/action', methods=['POST'])
@token_required
@admin_required
def admin_action(current_user):
    data = request.json
    action, target_id = data.get('action'), data.get('target_id')
    db = get_db()
    c = db.cursor()
    
    if action == 'toggle_ban': c.execute("UPDATE users SET is_banned = NOT is_banned WHERE id=%s", (target_id,))
    elif action == 'verify_donor': c.execute("UPDATE donors SET is_verified=TRUE WHERE id=%s", (target_id,))
    elif action == 'approve_payment':
        c.execute("SELECT request_id FROM payments WHERE id=%s", (target_id,))
        req_id = c.fetchone()['request_id']
        c.execute("UPDATE requests SET is_priority=TRUE WHERE id=%s", (req_id,))
        c.execute("UPDATE payments SET status='approved' WHERE id=%s", (target_id,))
    elif action == 'close_enquiry': c.execute("UPDATE enquiries SET status='closed' WHERE id=%s", (target_id,))
    
    db.commit()
    return jsonify({"success": True})

if __name__ == '__main__':
    socketio.run(app, debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
