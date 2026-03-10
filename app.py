from flask import Flask, render_template, request, jsonify, session, redirect, url_for, make_response
import os
from datetime import datetime, timedelta
import pyotp
import calendar
import pandas as pd
import io
from supabase import create_client, Client
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo # Add this import
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

# ---------------- CONFIG ----------------



# Replace with your Supabase credentials
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

SHARED_SECRET = os.getenv("SHARED_SECRET")
totp = pyotp.TOTP(SHARED_SECRET, interval=30)
REQUIRED_HOURS = int(os.getenv("REQUIRED_HOURS", 7))

CLASSROOM_POLYGON = [
    (15.778169, 74.460522),  #vtu
    (15.778282, 74.465453),
    (15.774238, 74.466045),
    (15.774311, 74.459154)
]
# CLASSROOM_POLYGON = [
#     (15.877109, 74.519842),
#     (15.878934, 74.520693),
#     (15.877820, 74.523730),
#     (15.874640, 74.521714)
# ]
app.secret_key = os.getenv("SECRET_KEY")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

# --- AUTH DECORATOR ---
def login_required(f):
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

def get_ist_now():
    """Returns the current time in Indian Standard Time"""
    return datetime.now(ZoneInfo("Asia/Kolkata"))

@app.route('/admin/login', methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password")
        if password == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        return render_template("login.html", error="Invalid Password")
    return render_template("login.html")

@app.route('/admin/logout')
def admin_logout():
    session.pop("logged_in", None)
    return redirect(url_for("admin_login"))

# ---------------- GEOFENCE ----------------

def point_in_polygon(lat, lon, polygon):
    x, y = lon, lat
    inside = False
    n = len(polygon)
    p1x, p1y = polygon[0][1], polygon[0][0]

    for i in range(n + 1):
        p2x, p2y = polygon[i % n][1], polygon[i % n][0]
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x, p1y = p2x, p2y
    return inside

# ---------------- PUBLIC ROUTES ----------------

@app.route('/')
def index():
    return render_template("index.html")

@app.route('/get_token')
def get_token():
    return jsonify({
        "token": totp.now(),
        "expires_in": 30 - (int(datetime.now().timestamp()) % 30)
    })

@app.route('/checkin')
def checkin():
    token = request.args.get("token")
    return render_template("checkin.html", token=token)

# ---------------- CORE LOGIC (LOGIN / LOGOUT) ----------------

@app.route('/submit', methods=["POST"])
def submit():
    usn = request.form.get("usn", "").strip().upper()
    name = request.form.get("name", "").strip()
    token = request.form.get("token")
    device_id = request.form.get("device_id")

    if not device_id:
        return jsonify({"status": "error", "message": "Device ID missing"})

    try:
        lat = float(request.form.get("lat"))
        lon = float(request.form.get("lon"))
    except:
        return jsonify({"status": "error", "message": "GPS Missing"})

    if not totp.verify(token, valid_window=1):
        return jsonify({"status": "error", "message": "QR Expired"})

    if not point_in_polygon(lat, lon, CLASSROOM_POLYGON):
        return jsonify({"status": "error", "message": "Outside Classroom Area"})

    # 1. Dynamic User Registration
    user_res = supabase.table("users").select("*").eq("usn", usn).execute()
    if not user_res.data:
        if not name:
            return jsonify({"status": "new_user", "message": "USN not found. Enter your name to register."})
        supabase.table("users").insert({"usn": usn, "name": name}).execute()

    today = get_ist_now().strftime("%Y-%m-%d")
    att_res = supabase.table("attendance").select("*").eq("usn", usn).eq("date", today).execute()
    record = att_res.data[0] if att_res.data else None

    # FIRST LOGIN
    if not record:
        device_used = supabase.table("attendance").select("*").eq("date", today).eq("device_id", device_id).execute()
        if device_used.data:
            return jsonify({"status": "error", "message": "This device already used today."})

        supabase.table("attendance").insert({
            "usn": usn, "date": today, "login_time": get_ist_now().strftime("%H:%M:%S"),
            "status": "On Duty", "device_id": device_id
        }).execute()
        return jsonify({"status": "login", "message": "Login Successful"})

    # ASK LOGOUT WITH 7 HOUR LOCK
    elif record.get("logout_time") is None:
        if record.get("device_id") != device_id:
            return jsonify({"status": "error", "message": "Logout allowed only from same device."})

        login_dt = datetime.strptime(f"{today} {record['login_time']}", "%Y-%m-%d %H:%M:%S")
        elapsed = get_ist_now().replace(tzinfo=None) - login_dt
        required_delta = timedelta(hours=REQUIRED_HOURS)

        if elapsed < required_delta:
            remaining = required_delta - elapsed
            rem_hrs, rem_mins = divmod(remaining.seconds // 60, 60)
            return jsonify({
                "status": "early", 
                "message": f"Too early! You must stay for {REQUIRED_HOURS} hours. Remaining: {rem_hrs}h {rem_mins}m"
            })

        return jsonify({"status": "confirm_logout", "message": "7 hours completed. Logout now?"})

    # COMPLETED
    else:
        return jsonify({"status": "done", "message": "Attendance Already Completed"})

@app.route('/logout', methods=["POST"])
def logout():
    usn = request.form.get("usn", "").strip().upper()
    device_id = request.form.get("device_id")
    today = get_ist_now().strftime("%Y-%m-%d")

    att_res = supabase.table("attendance").select("*").eq("usn", usn).eq("date", today).execute()
    record = att_res.data[0] if att_res.data else None

    if not record or record.get("device_id") != device_id:
        return jsonify({"status": "error", "message": "Logout not allowed."})

    supabase.table("attendance").update({
        "logout_time": get_ist_now().strftime("%H:%M:%S"), "status": "P"
    }).eq("usn", usn).eq("date", today).execute()

    return jsonify({"status": "success", "message": "Logout Successful"})

# ---------------- ADMIN DASHBOARD & CSV ----------------

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    filter_val = request.args.get('filter', get_ist_now().strftime('%Y-%m-%d'))
    
    # FIX: Use .eq() instead of .like() because we are matching an exact DATE type
    res = supabase.table("attendance").select("*, users(name)").eq("date", filter_val).order("login_time", desc=True).execute()
    
    # Flatten the result for the template
    records = []
    for r in res.data:
        r['name'] = r['users']['name'] if r.get('users') else "Unknown"
        records.append(r)
        
    return render_template("admin_dashboard.html", records=records, current_filter=filter_val)

@app.route('/admin/upload_csv', methods=["POST"])
@login_required
def upload_users_csv():
    if 'file' not in request.files: return redirect(url_for('admin_dashboard'))
    file = request.files['file']
    if file.filename == '': return redirect(url_for('admin_dashboard'))

    df = pd.read_csv(file)
    # Ensure column names match Supabase table (usn, name)
    records = df[['USN', 'Name']].rename(columns={'USN': 'usn', 'Name': 'name'}).to_dict('records')
    
    for rec in records:
        supabase.table("users").upsert(rec).execute()
        
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/download_csv')
@login_required
def download_csv():
    
# New
    filter_val = request.args.get('filter', get_ist_now().strftime('%Y-%m-%d'))
    mode = request.args.get('mode', 'full')
    now = datetime.now()

    try:
        year, month = map(int, filter_val.split('-')[:2])
    except:
        year, month = now.year, now.month

    # Calculate the last day of the month for our query
    last_day_of_month = calendar.monthrange(year, month)[1]

    if year == now.year and month == now.month:
        num_days = now.day
    else:
        num_days = last_day_of_month

    # Fetch users from Supabase
    users_res = supabase.table("users").select("name, usn").execute()
    all_users = pd.DataFrame(users_res.data)
    if not all_users.empty:
        all_users.rename(columns={'name': 'Name', 'usn': 'USN'}, inplace=True)
    else:
        all_users = pd.DataFrame(columns=['Name', 'USN'])

    # FIX: Use date ranges (.gte and .lte) instead of .like() for PostgreSQL DATE types
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month:02d}-{last_day_of_month}"
    
    att_res = supabase.table("attendance").select("usn, date, login_time, logout_time, status").gte("date", start_date).lte("date", end_date).execute()
    attendance_data = pd.DataFrame(att_res.data)

    output = io.StringIO()
    final_df = all_users.copy()

    if not attendance_data.empty:
        for day in range(1, num_days + 1):
            date_str = f"{year}-{month:02d}-{day:02d}"
            day_data = attendance_data[attendance_data['date'] == date_str].copy()
            
            if mode == 'full':
                subset = day_data[['usn', 'login_time', 'logout_time', 'status']]
                subset.columns = ['usn', f'{day}_Login', f'{day}_Logout', f'{day}_Status']
            else:
                subset = day_data[['usn', 'status']]
                subset.columns = ['usn', f'{day}_Status']
            
            final_df = pd.merge(final_df, subset, left_on='USN', right_on='usn', how='left')
            final_df[f'{day}_Status'] = final_df[f'{day}_Status'].fillna('A')
            if 'usn' in final_df.columns:
                final_df.drop(columns=['usn'], inplace=True)

    final_df.to_csv(output, index=False)
    
    clean_date_label = f"{year}-{month:02d}"
    filename = f"Attendance_{'Full' if mode == 'full' else 'Grid'}_{clean_date_label}.csv"
    
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    response.headers["Content-type"] = "text/csv"
    return response
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)