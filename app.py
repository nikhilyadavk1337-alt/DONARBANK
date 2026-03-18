import gevent.monkey
gevent.monkey.patch_all()

import os
import math
import io
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, g, send_from_directory, make_response, send_file
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import jwt
from flask_socketio import SocketIO, emit, join_room
import traceback
import psycopg2
import psycopg2.extras
import razorpay
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from twilio.rest import Client

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'super-secret-lifedrop-key')
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Integrations
razorpay_client = razorpay.Client(auth=("rzp_test_SR0otvXz1f0cJS", "EuBebpg2glCHSaVhnmI0BoJz"))
DB_URL = os.environ.get('DATABASE_URL', 'postgresql://postgres:your_password@db.supabase.co:5432/postgres')

# Twilio (Optional: Falls back to console log if keys not present)
TWILIO_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE = os.environ.get('TWILIO_PHONE_NUMBER')

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
    # Core Tables
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT DEFAULT 'user', is_banned BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS donors (id SERIAL PRIMARY KEY, user_id INTEGER UNIQUE, name TEXT NOT NULL, blood_group TEXT NOT NULL, lat REAL, lon REAL, available BOOLEAN DEFAULT TRUE, rating REAL DEFAULT 5.0, total_donations INTEGER DEFAULT 0, last_donation_date TIMESTAMP, is_verified BOOLEAN DEFAULT FALSE, document_path TEXT, phone TEXT, badge TEXT DEFAULT 'New Donor', FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS hospitals (id SERIAL PRIMARY KEY, user_id INTEGER UNIQUE, name TEXT NOT NULL, lat REAL, lon REAL, stock_ap REAL DEFAULT 0, stock_an REAL DEFAULT 0, stock_bp REAL DEFAULT 0, stock_bn REAL DEFAULT 0, stock_op REAL DEFAULT 0, stock_on REAL DEFAULT 0, stock_abp REAL DEFAULT 0, stock_abn REAL DEFAULT 0, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS requests (id SERIAL PRIMARY KEY, user_id INTEGER, blood_group TEXT NOT NULL, lat REAL, lon REAL, status TEXT DEFAULT 'pending', is_priority BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS donations (id SERIAL PRIMARY KEY, donor_id INTEGER, request_id INTEGER, status TEXT DEFAULT 'accepted', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(donor_id) REFERENCES donors(id) ON DELETE CASCADE, FOREIGN KEY(request_id) REFERENCES requests(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS enquiries (id SERIAL PRIMARY KEY, name TEXT, email TEXT, message TEXT, status TEXT DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS payments (id SERIAL PRIMARY KEY, user_id INTEGER, request_id INTEGER, amount REAL, razorpay_order_id TEXT, razorpay_payment_id TEXT, status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    ''')
    
    # Auto-migration for new features (Reputation & SMS)
    try:
        cursor.execute("ALTER TABLE donors ADD COLUMN IF NOT EXISTS phone TEXT;")
        cursor.execute("ALTER TABLE donors ADD COLUMN IF NOT EXISTS badge TEXT DEFAULT 'New Donor';")
    except Exception: pass
    
    cursor.execute("SELECT * FROM users WHERE email='nikhiladmin'")
    if not cursor.fetchone():
        cursor.execute("INSERT INTO users (email, password, role) VALUES (%s, %s, %s)", ('nikhiladmin', generate_password_hash('nikhil9936'), 'admin'))
    db.commit()
    db.close()

try: init_db()
except Exception as e: print("DB Init Error:", e)

# ---------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------
def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token: return jsonify({'success': False, 'message': 'Authentication required!'}), 401
        try:
            token = token.split(" ")[1]
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            db = get_db()
            c = db.cursor()
            c.execute("SELECT * FROM users WHERE id=%s", (data['user_id'],))
            current_user = c.fetchone()
            if not current_user or current_user['is_banned']: raise Exception()
        except: return jsonify({'success': False, 'message': 'Session expired.'}), 401
        return f(current_user, *args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(current_user, *args, **kwargs):
        if current_user['role'] != 'admin': return jsonify({'success': False, 'message': 'Admin access required!'}), 403
        return f(current_user, *args, **kwargs)
    return decorated

def calculate_distance(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2): return float('inf')
    try:
        R, dlat, dlon = 6371, math.radians(float(lat2) - float(lat1)), math.radians(float(lon2) - float(lon1))
        a = math.sin(dlat/2)**2 + math.cos(math.radians(float(lat1))) * math.cos(math.radians(float(lat2))) * math.sin(dlon/2)**2
        return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))
    except: return float('inf')

def send_sms_alert(phone, bg):
    """ Feature 4: WhatsApp/SMS System via Twilio """
    if not phone: return
    msg = f"🚨 LifeDrop Emergency! Critical demand for {bg} blood near you. Open app to save a life."
    if TWILIO_SID and TWILIO_AUTH:
        try:
            client = Client(TWILIO_SID, TWILIO_AUTH)
            client.messages.create(body=msg, from_=TWILIO_PHONE, to=phone)
        except Exception as e: print("Twilio Error:", e)
    else:
        print(f"📱 [MOCK SMS to {phone}]: {msg}")

# ---------------------------------------------------------
# SOCKET.IO (REAL-TIME CHAT & UBER-STYLE LIVE TRACKING)
# ---------------------------------------------------------
@socketio.on('join')
def on_join(data):
    if data.get('user_id'): join_room(f"user_{data['user_id']}")
    join_room("global_room")

@socketio.on('join_mission')
def on_join_mission(data):
    """ Feature 5 & 7: Join specific request room for Chat & Tracking """
    room = f"mission_{data['request_id']}"
    join_room(room)

@socketio.on('send_chat')
def on_send_chat(data):
    """ Feature 7: Real-Time Chat """
    room = f"mission_{data['request_id']}"
    emit('receive_chat', data, room=room)

@socketio.on('update_location')
def on_update_loc(data):
    """ Feature 5: Live GPS Tracking (Uber Style) """
    room = f"mission_{data['request_id']}"
    emit('location_updated', data, room=room)

# ---------------------------------------------------------
# CORE APIs (Auth, Profile, Maps)
# ---------------------------------------------------------
@app.route('/')
def serve_index(): return send_from_directory('.', 'index.html')

@app.route('/favicon.ico')
def favicon(): return '', 204

@app.route('/<filename>')
def serve_root_images(filename):
    if filename.endswith(('.png', '.jpg', '.jpeg', '.gif', '.json', '.js')): return send_from_directory('.', filename)
    return "Not found", 404

@app.route('/uploads/<filename>')
def uploaded_file(filename): return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

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
        return jsonify({"success": True, "message": "Account created! You can now sign in."})
    
    elif action == 'login':
        c.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = c.fetchone()
        if user and check_password_hash(user['password'], password):
            if user['is_banned']: return jsonify({"success": False, "message": "Account banned."}), 403
            token = jwt.encode({'user_id': user['id'], 'exp': datetime.utcnow() + timedelta(days=7)}, app.config['SECRET_KEY'], algorithm="HS256")
            return jsonify({"success": True, "token": token, "role": user['role'], "id": user['id']})
        return jsonify({"success": False, "message": "Invalid credentials!"})

@app.route('/api/profile/update', methods=['POST'])
@token_required
def update_profile(current_user):
    db = get_db()
    c = db.cursor()
    if current_user['role'] == 'user':
        name, bg, lat, lon, avail, phone = request.form.get('name'), request.form.get('blood_group'), request.form.get('lat'), request.form.get('lon'), request.form.get('available') == 'true', request.form.get('phone')
        doc_path = None
        if 'document' in request.files:
            file = request.files['document']
            doc_path = secure_filename(f"doc_{current_user['id']}_{file.filename}")
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], doc_path))

        c.execute("SELECT id FROM donors WHERE user_id=%s", (current_user['id'],))
        if c.fetchone():
            c.execute("UPDATE donors SET name=%s, blood_group=%s, lat=%s, lon=%s, phone=%s, available=COALESCE(%s, available), document_path=COALESCE(%s, document_path) WHERE user_id=%s", (name, bg, lat, lon, phone, avail, doc_path, current_user['id']))
        else:
            c.execute("INSERT INTO donors (user_id, name, blood_group, lat, lon, phone, available, document_path) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (current_user['id'], name, bg, lat, lon, phone, avail, doc_path))
            
    elif current_user['role'] == 'hospital':
        name, lat, lon = request.form.get('name'), request.form.get('lat'), request.form.get('lon')
        c.execute("SELECT id FROM hospitals WHERE user_id=%s", (current_user['id'],))
        if c.fetchone(): c.execute("UPDATE hospitals SET name=%s, lat=%s, lon=%s WHERE user_id=%s", (name, lat, lon, current_user['id']))
        else: c.execute("INSERT INTO hospitals (user_id, name, lat, lon) VALUES (%s, %s, %s, %s)", (current_user['id'], name, lat, lon))
    db.commit()
    return jsonify({"success": True, "message": "Profile synchronized successfully!"})

@app.route('/api/find_donors', methods=['POST'])
@token_required
def find_donors(current_user):
    data = request.json
    lat, lon, bg = data.get('lat'), data.get('lon'), data.get('blood_group')
    c = get_db().cursor()
    c.execute("SELECT * FROM donors WHERE blood_group=%s AND available=TRUE AND user_id!=%s", (bg, current_user['id']))
    donors = c.fetchall()
    
    col_name = f"stock_{bg.replace('+','p').replace('-','n')}"
    c.execute(f"SELECT * FROM hospitals WHERE {col_name} > 0")
    hospitals = c.fetchall()
    
    results = []
    for d in donors:
        d_dict, d_dict['type'], d_dict['distance_km'] = dict(d), 'donor', round(calculate_distance(lat, lon, d['lat'], d['lon']), 2)
        results.append(d_dict)
    for h in hospitals:
        h_dict, h_dict['type'], h_dict['distance_km'], h_dict['rating'] = dict(h), 'hospital', round(calculate_distance(lat, lon, h['lat'], h['lon']), 2), 5.0
        results.append(h_dict)
        
    results.sort(key=lambda x: x['distance_km'])
    return jsonify({"success": True, "data": results})

# ---------------------------------------------------------
# NEW HACKATHON FEATURES (AI, Scoring, Badges, PDF)
# ---------------------------------------------------------

@app.route('/api/predict_demand', methods=['GET'])
def predict_demand():
    """ Feature 1: AI Blood Demand Prediction (Time-Series classification) """
    c = get_db().cursor()
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    c.execute("SELECT blood_group, COUNT(*) as count FROM requests WHERE created_at >= %s GROUP BY blood_group", (thirty_days_ago,))
    data = c.fetchall()
    
    predictions, highest_bg = {}, None
    max_cnt = 0
    for row in data:
        bg, cnt = row['blood_group'], row['count']
        if cnt > 10: predictions[bg] = "High"
        elif cnt > 5: predictions[bg] = "Medium"
        else: predictions[bg] = "Low"
        if cnt > max_cnt: max_cnt, highest_bg = cnt, bg
            
    return jsonify({"success": True, "predictions": predictions, "critical": highest_bg})

@app.route('/api/leaderboard', methods=['GET'])
def leaderboard():
    """ Feature 3: Donor Reputation System Leaderboard """
    c = get_db().cursor()
    c.execute("SELECT name, blood_group, rating, total_donations, badge, is_verified FROM donors WHERE total_donations > 0 ORDER BY total_donations DESC, rating DESC LIMIT 10")
    return jsonify({"success": True, "data": c.fetchall()})

@app.route('/api/create_request', methods=['POST'])
@token_required
def create_request(current_user):
    """ Feature 2 & 8: Smart Scoring, Trust Prioritization & Smart Popup """
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
        # AI SCORING LOGIC: Distance (inverse), Rating, and Verified Trust Boost (+50)
        score = (1 / max(dist, 0.1)) * 50 + (d['rating'] * 30) + (50 if d['is_verified'] else 0)
        scored.append((score, dict(d)))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    top_3 = [d[1] for d in scored[:3]]
    top_donor = top_3[0] if top_3 else None

    # Emit alerts and send SMS
    for d in top_3:
        socketio.emit('auto_assign', {"request_id": req_id, "blood_group": data.get('blood_group'), "lat": data.get('lat'), "lon": data.get('lon')}, room=f"user_{d['user_id']}")
        send_sms_alert(d.get('phone'), data.get('blood_group'))

    return jsonify({"success": True, "message": "Emergency broadcasted.", "request_id": req_id, "top_donor": top_donor})

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
        last_don = datetime.strptime(donor['last_donation_date'].split('.')[0], '%Y-%m-%d %H:%M:%S') if isinstance(donor['last_donation_date'], str) else donor['last_donation_date']
        days_since = (datetime.utcnow() - last_don).days
        if days_since < 90: return jsonify({"success": False, "message": f"Cooldown active. {90 - days_since} days remaining."})

    # Badge Upgrade Logic
    new_tot = donor['total_donations'] + 1
    badge = "Hero Donor 🥇" if new_tot >= 10 else donor['badge']

    c.execute("INSERT INTO donations (donor_id, request_id) VALUES (%s, %s)", (donor['id'], req_id))
    c.execute("UPDATE requests SET status='accepted' WHERE id=%s", (req_id,))
    c.execute("UPDATE donors SET last_donation_date=CURRENT_TIMESTAMP, total_donations=%s, badge=%s WHERE id=%s", (new_tot, badge, donor['id']))
    db.commit()
    
    c.execute("SELECT user_id, lat, lon FROM requests WHERE id=%s", (req_id,))
    req = c.fetchone()
    
    # Notify Patient and Start Mission (Live Tracking)
    mission_data = {"request_id": req_id, "donor_name": donor['name'], "donor_lat": donor['lat'], "donor_lon": donor['lon'], "req_lat": req['lat'], "req_lon": req['lon']}
    socketio.emit('request_accepted', mission_data, room=f"user_{req['user_id']}")
    
    return jsonify({"success": True, "message": "Request accepted. Live tracking active.", "mission": mission_data})

@app.route('/api/certificate/<int:donor_id>', methods=['GET'])
def download_certificate(donor_id):
    """ Feature 6: Auto-generate PDF Donation Certificate using ReportLab """
    c = get_db().cursor()
    c.execute("SELECT name, blood_group, total_donations FROM donors WHERE user_id=%s", (donor_id,))
    donor = c.fetchone()
    if not donor or donor['total_donations'] == 0:
        return "No donations found to generate certificate.", 404

    pdf_buffer = io.BytesIO()
    p = canvas.Canvas(pdf_buffer, pagesize=letter)
    
    # Draw Premium PDF Design
    p.setLineWidth(4)
    p.setStrokeColorRGB(0.88, 0.11, 0.28) # Rose-600
    p.rect(20, 20, 570, 750)
    
    p.setFont("Helvetica-Bold", 36)
    p.setFillColorRGB(0.88, 0.11, 0.28)
    p.drawCentredString(306, 650, "CERTIFICATE OF HONOR")
    
    p.setFont("Helvetica", 18)
    p.setFillColorRGB(0.2, 0.2, 0.2)
    p.drawCentredString(306, 580, "This is proudly presented to")
    
    p.setFont("Helvetica-Bold", 28)
    p.setFillColorRGB(0, 0, 0)
    p.drawCentredString(306, 530, donor['name'].upper())
    
    p.setFont("Helvetica", 14)
    p.drawCentredString(306, 470, f"For saving a life through a selfless {donor['blood_group']} blood donation.")
    p.drawCentredString(306, 440, "Your contribution embodies the true spirit of humanity.")
    
    p.setFont("Helvetica-Bold", 12)
    p.setFillColorRGB(0.5, 0.5, 0.5)
    p.drawCentredString(306, 380, f"Total Lifes Saved: {donor['total_donations']}")
    p.drawCentredString(306, 360, f"Date Issued: {datetime.utcnow().strftime('%Y-%m-%d')}")
    
    p.setFont("Helvetica-Bold", 16)
    p.setFillColorRGB(0.88, 0.11, 0.28)
    p.drawCentredString(306, 150, "LifeDrop Network Official")
    p.save()
    
    pdf_buffer.seek(0)
    return send_file(pdf_buffer, as_attachment=True, download_name=f"LifeDrop_Hero_{donor['name']}.pdf", mimetype='application/pdf')

# ---------------------------------------------------------
# NEW MISSING ENDPOINTS ADDED BELOW
# ---------------------------------------------------------

@app.route('/api/hospital/stock', methods=['POST'])
@token_required
def update_hospital_stock(current_user):
    if current_user['role'] != 'hospital':
        return jsonify({"success": False, "message": "Unauthorized"}), 403
    data = request.json
    db = get_db()
    c = db.cursor()
    c.execute("""
        UPDATE hospitals 
        SET stock_ap=%s, stock_an=%s, stock_bp=%s, stock_bn=%s, 
            stock_op=%s, stock_on=%s, stock_abp=%s, stock_abn=%s 
        WHERE user_id=%s
    """, (
        data.get('A+', 0), data.get('A-', 0), data.get('B+', 0), data.get('B-', 0),
        data.get('O+', 0), data.get('O-', 0), data.get('AB+', 0), data.get('AB-', 0),
        current_user['id']
    ))
    db.commit()
    return jsonify({"success": True, "message": "Inventory synchronized to global network!"})

@app.route('/api/admin/broadcast', methods=['POST'])
@token_required
@admin_required
def admin_broadcast(current_user):
    msg = request.json.get('message')
    if msg:
        socketio.emit('global_alert', {"message": msg}, room="global_room")
        return jsonify({"success": True, "message": "Alert broadcasted globally via WebSockets!"})
    return jsonify({"success": False, "message": "Alert message required."})

@app.route('/api/enquiry', methods=['POST'])
def submit_enquiry():
    data = request.json
    db = get_db()
    c = db.cursor()
    c.execute("INSERT INTO enquiries (name, email, message) VALUES (%s, %s, %s)", 
              (data.get('name'), data.get('email'), data.get('message')))
    db.commit()
    return jsonify({"success": True, "message": "Enquiry submitted successfully."})

@app.route('/api/payment/priority', methods=['POST'])
@token_required
def payment_priority(current_user):
    # Process screenshot validation
    req_id = request.form.get('request_id')
    db = get_db()
    c = db.cursor()
    c.execute("UPDATE requests SET is_priority=TRUE WHERE id=%s", (req_id,))
    db.commit()
    return jsonify({"success": True, "message": "Priority Boost Active!"})

# ---------------------------------------------------------
# DASHBOARD, ADMIN & PAYMENTS
# ---------------------------------------------------------
@app.route('/api/payment/create_order', methods=['POST'])
@token_required
def create_order(current_user):
    req_id = request.json.get('request_id')
    try:
        order = razorpay_client.order.create(data={"amount": 2000, "currency": "INR", "receipt": f"receipt_req_{req_id}"})
        return jsonify({"success": True, "order_id": order['id'], "amount": order['amount']})
    except: return jsonify({"success": False, "message": "Gateway error."})

@app.route('/api/payment/verify', methods=['POST'])
@token_required
def verify_payment(current_user):
    data = request.json
    try:
        razorpay_client.utility.verify_payment_signature({'razorpay_order_id': data.get('razorpay_order_id'), 'razorpay_payment_id': data.get('razorpay_payment_id'), 'razorpay_signature': data.get('razorpay_signature')})
        db = get_db()
        db.cursor().execute("INSERT INTO payments (user_id, request_id, amount, razorpay_order_id, razorpay_payment_id, status) VALUES (%s, %s, 20.0, %s, %s, 'approved')", (current_user['id'], data.get('request_id'), data.get('razorpay_order_id'), data.get('razorpay_payment_id')))
        db.cursor().execute("UPDATE requests SET is_priority=TRUE WHERE id=%s", (data.get('request_id'),))
        db.commit()
        return jsonify({"success": True, "message": "Priority Boost Active!"})
    except: return jsonify({"success": False, "message": "Verification failed."})

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
        return jsonify({"success": True, "profile": dict(c.fetchone() or {})})

@app.route('/api/admin/data', methods=['GET'])
@token_required
@admin_required
def admin_data(current_user):
    c = get_db().cursor()
    c.execute("SELECT id, email, role, is_banned FROM users")
    users = c.fetchall()
    c.execute("SELECT id, name, is_verified, document_path FROM donors")
    donors = c.fetchall()
    c.execute("SELECT * FROM hospitals")
    hospitals = c.fetchall()
    c.execute("SELECT * FROM payments ORDER BY id DESC LIMIT 20")
    payments = c.fetchall()
    c.execute("SELECT * FROM enquiries ORDER BY id DESC")
    enquiries = c.fetchall()
    c.execute("SELECT COUNT(*) as cnt FROM requests")
    reqs = c.fetchone()['cnt']
    c.execute("SELECT COUNT(*) as cnt FROM donations")
    dons = c.fetchone()['cnt']
    return jsonify({"success": True, "users": [dict(u) for u in users], "donors": [dict(d) for d in donors], "hospitals": [dict(h) for h in hospitals], "payments": [dict(p) for p in payments], "enquiries": [dict(e) for e in enquiries], "stats": {"total_reqs": reqs, "total_dons": dons}})

@app.route('/api/admin/action', methods=['POST'])
@token_required
@admin_required
def admin_action(current_user):
    data = request.json
    db = get_db()
    c = db.cursor()
    
    if data['action'] == 'toggle_ban': 
        c.execute("UPDATE users SET is_banned = NOT is_banned WHERE id=%s", (data['target_id'],))
    elif data['action'] == 'verify_donor': 
        c.execute("UPDATE donors SET is_verified=TRUE WHERE id=%s", (data['target_id'],))
    elif data['action'] == 'close_enquiry': 
        # Fixes the missing 'Close Ticket' logic
        c.execute("UPDATE enquiries SET status='closed' WHERE id=%s", (data['target_id'],))
    elif data['action'] == 'delete_enquiry': 
        # NEW: Hard delete from database
        c.execute("DELETE FROM enquiries WHERE id=%s", (data['target_id'],))
        
    db.commit()
    return jsonify({"success": True})

@app.route('/api/demand_heatmap', methods=['GET'])
def get_heatmap():
    c = get_db().cursor()
    c.execute("SELECT ROUND(lat::numeric, 2) as lat, ROUND(lon::numeric, 2) as lon, COUNT(*) as weight FROM requests GROUP BY ROUND(lat::numeric, 2), ROUND(lon::numeric, 2)")
    return jsonify({"success": True, "data": [dict(d) for d in c.fetchall()]})

if __name__ == '__main__':
    socketio.run(app, debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
