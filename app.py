from flask import Flask, render_template, request, jsonify, session, redirect, url_for, make_response
import os
import calendar
import pandas as pd
import io
import pyotp
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

# ---------------- CONFIG ----------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

SHARED_SECRET = os.getenv("SHARED_SECRET")
totp = pyotp.TOTP(SHARED_SECRET, interval=30)

CLASSROOM_POLYGON = [
    (15.778169, 74.460522), (15.778282, 74.465453),
    (15.774238, 74.466045), (15.774311, 74.459154)
]

app.secret_key = os.getenv("SECRET_KEY")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
REQUIRED_HOURS = int(os.getenv("REQUIRED_HOURS", 7))



# ---------------- HELPERS ----------------
def login_required(f):
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"): return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

def get_ist_now():
    return datetime.now(ZoneInfo("Asia/Kolkata"))

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
    return jsonify({"token": totp.now(), "expires_in": 30 - (int(datetime.now().timestamp()) % 30)})

@app.route('/checkin')
def checkin():
    domains_res = supabase.table("domains").select("name").execute()
    batches_res = supabase.table("batches").select("name").execute()
    return render_template(
        "checkin.html", 
        token=request.args.get("token"),
        domains=[d['name'] for d in domains_res.data],
        batches=[b['name'] for b in batches_res.data]
    )

# ---------------- CORE LOGIC (STATE MACHINE) ----------------
@app.route('/submit', methods=["POST"])
def submit():
    usn = request.form.get("usn", "").strip().upper()
    name = request.form.get("name", "").strip()
    domain = request.form.get("domain", "").strip()
    batch = request.form.get("batch", "").strip()
    token = request.form.get("token")
    device_id = request.form.get("device_id")

    if not device_id: return jsonify({"status": "error", "message": "Device ID missing"})
    if not totp.verify(token, valid_window=1): return jsonify({"status": "error", "message": "QR Expired"})
    
    try:
        lat, lon = float(request.form.get("lat")), float(request.form.get("lon"))
        if not point_in_polygon(lat, lon, CLASSROOM_POLYGON):
            return jsonify({"status": "error", "message": "Outside Classroom Area"})
    except:
        return jsonify({"status": "error", "message": "GPS Missing"})

    now = get_ist_now()
    today_str = now.strftime("%Y-%m-%d")
    current_time = now.time()
    current_time_str = now.strftime("%H:%M:%S")

    # Strict 1 Device = 1 User Per Day Check
    device_check = supabase.table("attendance").select("usn").eq("date", today_str).eq("device_id", device_id).execute()
    if device_check.data:
        used_by_usns = set(row['usn'] for row in device_check.data)
        if usn not in used_by_usns or len(used_by_usns) > 1:
            return jsonify({
                "status": "error", 
                "message": "Security Alert: This device has already been used by another student today."
            })

    # Dynamic User Registration
    user_res = supabase.table("users").select("*").eq("usn", usn).execute()
    if not user_res.data:
        if not name or not domain or not batch: 
            return jsonify({"status": "new_user", "message": "Enter your details to register."})
        supabase.table("users").insert({"usn": usn, "name": name, "domain": domain, "batch": batch}).execute()

    att_res = supabase.table("attendance").select("*").eq("usn", usn).eq("date", today_str).execute()
    record = att_res.data[0] if att_res.data else None

    # ACTION 1: Morning Login
    if not record:
        if current_time >= time(13, 30):
            return jsonify({"status": "error", "message": "Too late for morning login."})
        supabase.table("attendance").insert({
            "usn": usn, "date": today_str, "login_time": current_time_str,
            "status": "On Duty", "device_id": device_id
        }).execute()
        return jsonify({"status": "success", "message": "Morning Login Successful!"})

    # ACTION 2: Lunch Start (13:20 - 13:50)
    if time(13, 20) <= current_time <= time(13, 50):
        if record.get("lunch_start"): return jsonify({"status": "error", "message": "Lunch start already recorded."})
        supabase.table("attendance").update({"lunch_start": current_time_str}).eq("id", record["id"]).execute()
        return jsonify({"status": "success", "message": "Lunch Break Started."})

    # ACTION 3: Lunch End (14:15 - 14:45)
    if time(14, 15) <= current_time <= time(14, 45):
        if not record.get("lunch_start"): return jsonify({"status": "error", "message": "You didn't scan for Lunch Start!"})
        if record.get("lunch_end"): return jsonify({"status": "error", "message": "Lunch end already recorded."})
        supabase.table("attendance").update({"lunch_end": current_time_str}).eq("id", record["id"]).execute()
        return jsonify({"status": "success", "message": "Lunch Break Ended. Welcome back."})

    # ACTION 4: Final Logout (16:00 onwards)
    if current_time >= time(16, 0):
        if record.get("logout_time"): 
            return jsonify({"status": "done", "message": "Already logged out for today."})
        
        settings_res = supabase.table("admin_settings").select("*").execute()
        settings = {row['setting_key']: row['setting_value'] for row in settings_res.data}

        if settings.get('require_minimum_hours', True):
            # REQUIRED_HOURS = 7
            login_time_obj = datetime.strptime(record['login_time'], "%H:%M:%S").time()
            login_dt = datetime.combine(now.date(), login_time_obj)
            elapsed = now.replace(tzinfo=None) - login_dt
            required_delta = timedelta(hours=REQUIRED_HOURS)

            if elapsed < required_delta:
                remaining = required_delta - elapsed
                rem_hrs, rem_mins = divmod(remaining.seconds // 60, 60)
                return jsonify({
                    "status": "error", 
                    "message": f"Too early! You must complete {REQUIRED_HOURS} hours. Remaining: {rem_hrs}h {rem_mins}m"
                })

        has_start = bool(record.get("lunch_start"))
        has_end = bool(record.get("lunch_end"))

        if settings.get('strict_lunch', True):
            if not has_start or not has_end:
                return jsonify({"status": "error", "message": "Cannot logout: Lunch scans are missing!"})

        remarks_list = []
        if not has_start and not has_end: remarks_list.append("Missed both lunch scans")
        elif has_start and not has_end: remarks_list.append("Missed lunch end scan")
        elif not has_start and has_end: remarks_list.append("Missed lunch start scan")
        final_remark = ", ".join(remarks_list) if remarks_list else "Clear"

        supabase.table("attendance").update({
            "logout_time": current_time_str, "status": "P", "remarks": final_remark
        }).eq("id", record["id"]).execute()
        
        return jsonify({"status": "success", "message": "Final Logout Successful. Have a good evening!"})

    return jsonify({"status": "error", "message": "Scan is outside of permitted time windows."})

# ---------------- ADMIN ROUTES ----------------
@app.route('/admin/login', methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        return render_template("login.html", error="Invalid Password")
    return render_template("login.html")

@app.route('/admin/logout')
def admin_logout():
    session.pop("logged_in", None)
    return redirect(url_for("admin_login"))

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    filter_date = request.args.get('date', get_ist_now().strftime('%Y-%m-%d'))
    filter_domain = request.args.get('domain', 'All')
    filter_view = request.args.get('view', 'daily')

    settings_res = supabase.table("admin_settings").select("*").execute()
    settings = {row['setting_key']: row['setting_value'] for row in settings_res.data}

    domains_res = supabase.table("domains").select("name").execute()
    domains = [d['name'] for d in domains_res.data]
    
    batches_res = supabase.table("batches").select("name").execute()
    batches = [b['name'] for b in batches_res.data]

    res = supabase.table("attendance").select("*, users(name, domain, batch)").eq("date", filter_date).execute()

    records = []
    total_domain_count = 0
    for r in res.data:
        user_data = r.get('users') or {}
        r['name'] = user_data.get('name', 'Unknown')
        r['domain'] = user_data.get('domain', 'N/A')
        r['batch'] = user_data.get('batch', 'N/A')
        
        if filter_domain == 'All' or r['domain'] == filter_domain:
            records.append(r)
            total_domain_count += 1
            
    return render_template(
        "admin_dashboard.html", 
        records=records, current_date=filter_date, current_domain=filter_domain, current_view=filter_view,
        domains=domains, batches=batches, strict_lunch=settings.get('strict_lunch', True),
        require_minimum_hours=settings.get('require_minimum_hours', True), total_count=total_domain_count
    )

@app.route('/admin/toggle_setting', methods=["POST"])
@login_required
def toggle_setting():
    data = request.json
    supabase.table("admin_settings").update({"setting_value": data.get("setting_value")}).eq("setting_key", data.get("setting_key")).execute()
    return jsonify({"status": "success"})

@app.route('/admin/manage_list', methods=["POST"])
@login_required
def manage_list():
    data = request.json
    table = "domains" if data.get('type') == 'domain' else "batches"
    action = data.get('action')
    name = data.get('name').strip()

    if not name: return jsonify({"status": "error"})

    if action == 'add': supabase.table(table).insert({"name": name}).execute()
    elif action == 'delete': supabase.table(table).delete().eq("name", name).execute()
        
    return jsonify({"status": "success"})

@app.route('/admin/upload_csv', methods=["POST"])
@login_required
def upload_users_csv():
    if 'file' not in request.files: 
        return redirect(url_for('admin_dashboard', upload='error'))
    
    file = request.files['file']
    if file.filename == '':
        return redirect(url_for('admin_dashboard', upload='error'))

    try:
        # 1. Read CSV and clean data
        df = pd.read_csv(file).fillna("")
        df.columns = df.columns.str.lower().str.strip() 
        
        required_cols = ['usn', 'name', 'domain', 'batch']
        for col in required_cols:
            if col not in df.columns:
                df[col] = "" 
        
        # --- NEW: Format USNs and drop duplicates from the CSV itself ---
        df['usn'] = df['usn'].astype(str).str.strip().str.upper()
        df = df[df['usn'] != ""] # Drop rows where USN is entirely blank
        df = df.drop_duplicates(subset=['usn'], keep='last') # If a USN appears twice, keep the last one

        # 2. Extract unique domains and batches
        unique_domains = [str(d).strip() for d in df['domain'].unique() if str(d).strip()]
        unique_batches = [str(b).strip() for b in df['batch'].unique() if str(b).strip()]

        # 3. Fetch existing domains/batches and insert only NEW ones
        existing_domains = [d['name'] for d in supabase.table("domains").select("name").execute().data]
        new_domains = [{"name": d} for d in unique_domains if d not in existing_domains]
        if new_domains:
            supabase.table("domains").insert(new_domains).execute()

        existing_batches = [b['name'] for b in supabase.table("batches").select("name").execute().data]
        new_batches = [{"name": b} for b in unique_batches if b not in existing_batches]
        if new_batches:
            supabase.table("batches").insert(new_batches).execute()
                
        # 4. Prepare User Data for Bulk Upsert
        records = df[required_cols].to_dict('records')
        valid_users = []
        
        for rec in records:
            valid_users.append({
                'usn': rec['usn'],
                'name': str(rec['name']).strip(),
                'domain': str(rec['domain']).strip(),
                'batch': str(rec['batch']).strip()
            })
                
        # 5. Bulk Upsert (Now guaranteed to have no internal duplicates)
        if valid_users:
            supabase.table("users").upsert(valid_users, on_conflict="usn").execute()
            
        return redirect(url_for('admin_dashboard', upload='success'))
        
    except Exception as e:
        print(f"CSV Upload Error: {e}") 
        return redirect(url_for('admin_dashboard', upload='error'))
    
@app.route('/admin/download_csv')
@login_required
def download_csv():
    filter_date = request.args.get('date', get_ist_now().strftime('%Y-%m-%d'))
    mode = request.args.get('mode', 'full')
    now = get_ist_now()

    try: year, month = map(int, filter_date.split('-')[:2])
    except: year, month = now.year, now.month

    last_day = calendar.monthrange(year, month)[1]
    num_days = now.day if (year == now.year and month == now.month) else last_day

    users_res = supabase.table("users").select("name, usn, domain, batch").execute()
    all_users = pd.DataFrame(users_res.data)
    if not all_users.empty: all_users.rename(columns={'name': 'Name', 'usn': 'USN', 'domain': 'Domain', 'batch': 'Batch'}, inplace=True)
    else: all_users = pd.DataFrame(columns=['Name', 'USN', 'Domain', 'Batch'])

    start_date, end_date = f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last_day}"
    att_res = supabase.table("attendance").select("*").gte("date", start_date).lte("date", end_date).execute()
    attendance_data = pd.DataFrame(att_res.data)

    final_df = all_users.copy()
    output = io.StringIO()

    if not attendance_data.empty:
        for day in range(1, num_days + 1):
            date_str = f"{year}-{month:02d}-{day:02d}"
            day_data = attendance_data[attendance_data['date'] == date_str].copy()
            
            if mode == 'full':
                subset = day_data[['usn', 'login_time', 'lunch_start', 'lunch_end', 'logout_time', 'remarks', 'status']]
                subset.columns = ['usn', f'{day}_Login', f'{day}_LunchOut', f'{day}_LunchIn', f'{day}_Logout', f'{day}_Remarks', f'{day}_Status']
            elif mode == 'basic_remarks':
                subset = day_data[['usn', 'login_time', 'logout_time', 'remarks', 'status']]
                subset.columns = ['usn', f'{day}_Login', f'{day}_Logout', f'{day}_Remarks', f'{day}_Status']
            else:
                subset = day_data[['usn', 'status']]
                subset.columns = ['usn', f'{day}_Status']
            
            if not subset.empty:
                final_df = pd.merge(final_df, subset, left_on='USN', right_on='usn', how='left')
                if 'usn' in final_df.columns: final_df.drop(columns=['usn'], inplace=True)
            if f'{day}_Status' in final_df.columns:
                final_df[f'{day}_Status'] = final_df[f'{day}_Status'].fillna('A')

    final_df.to_csv(output, index=False)
    response = make_response(output.getvalue())
    response.headers["Content-Disposition"] = f"attachment; filename=Attendance_{mode.capitalize()}_{year}-{month:02d}.csv"
    response.headers["Content-type"] = "text/csv"
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)