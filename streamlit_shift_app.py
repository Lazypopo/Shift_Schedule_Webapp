
# streamlit_shift_app.py
# Streamlit app for ABCIE shift scheduling
# How to run: streamlit run streamlit_shift_app.py
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import streamlit as st
from io import BytesIO

st.set_page_config(page_title="ABCIE Shift Scheduler", layout="wide")

ZONES = ["A", "B", "C", "I", "E"]

# ---------- Database Utilities ----------
@st.cache_resource
def get_conn(db_path="shift_schedule.db"):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return conn

def setup_database(conn):
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            name TEXT PRIMARY KEY,
            points INTEGER DEFAULT 0,
            max_points INTEGER DEFAULT 10,
            off_days TEXT,
            preferred_zone TEXT,
            allowed_zones TEXT
        )
    """)
    # safe ALTERs
    try:
        cursor.execute("ALTER TABLE employees ADD COLUMN preferred_zone TEXT")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE employees ADD COLUMN allowed_zones TEXT")
    except Exception:
        pass
    conn.commit()

def add_or_update_employee(conn, name, initial_points=0, max_points=10,
                           off_days=None, preferred_zone=None, allowed_zones=None):
    if off_days is None:
        off_days = []
    if allowed_zones is None:
        allowed_zones = []
    off_days_str = ",".join(off_days)
    allowed_str = ",".join(allowed_zones)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO employees (name, points, max_points, off_days, preferred_zone, allowed_zones)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (name, initial_points, max_points, off_days_str, preferred_zone, allowed_str))
    cursor.execute("""
        UPDATE employees
           SET max_points = ?,
               off_days = ?,
               preferred_zone = ?,
               allowed_zones = ?
         WHERE name = ?
    """, (max_points, off_days_str, preferred_zone, allowed_str, name))
    conn.commit()

def get_employees(conn):
    df = pd.read_sql_query("SELECT * FROM employees", conn)
    if df.empty:
        return df
    df["off_days"] = df["off_days"].apply(lambda x: x.split(",") if x else [])
    df["allowed_zones"] = df["allowed_zones"].apply(lambda x: [s for s in (x.split(",") if x else []) if s])
    return df

def update_points(conn, name, points):
    cursor = conn.cursor()
    cursor.execute("UPDATE employees SET points = points + ? WHERE name = ?", (points, name))
    conn.commit()

def is_weekend(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d").weekday() >= 5

def generate_schedule(conn, start_date, end_date, apply_points=True):
    """
    5 zones (A,B,C,I,E) each need 1 person per day.
    Priority: preferred_zone match (True>False) → fewer current points → name
    Constraints: not on off_days; at most one shift within 3 days; not exceed max_points; zone in allowed_zones; not 2 shifts in the same day.
    """
    schedule = []
    unassigned = []
    assigned_last = {}
    # snapshot employees (points may change if apply_points=True)
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end     = datetime.strptime(end_date,   "%Y-%m-%d")

    # We'll track in-memory point tallies to make fair decisions even if not applying to DB yet
    base_df = get_employees(conn).copy()
    points_map = {r["name"]: int(r["points"]) if pd.notna(r["points"]) else 0 for _, r in base_df.iterrows()}

    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        point_value = 2 if is_weekend(date_str) else 1
        assigned_today = set()
        for zone in ZONES:
            df = get_employees(conn)
            if df.empty:
                unassigned.append({"Date": date_str, "Zone": zone, "Reason": "No employees"})
                continue
            candidates = []
            for _, r in df.iterrows():
                name = r["name"]
                points = points_map.get(name, 0)
                max_points = int(r["max_points"]) if pd.notna(r["max_points"]) else 0
                off_days = r["off_days"] or []
                allowed = set(r["allowed_zones"] or [])
                preferred = (r.get("preferred_zone") or "").strip()
                last = assigned_last.get(name)
                days_since_last = (current - last).days if last else 9999
                if (
                    date_str not in off_days and
                    name not in assigned_today and
                    days_since_last >= 3 and
                    (points + point_value) <= max_points and
                    zone in allowed
                ):
                    pref_match = (preferred == zone)
                    candidates.append((pref_match, points, name))
            candidates.sort(key=lambda t: (-int(t[0]), t[1], t[2]))
            if candidates:
                _, _, chosen = candidates[0]
                schedule.append({
                    "Date": date_str,
                    "Shift": zone,
                    "Employee": chosen,
                    "Points": point_value
                })
                assigned_last[chosen] = current
                assigned_today.add(chosen)
                # Update in-memory points and optionally DB
                points_map[chosen] = points_map.get(chosen, 0) + point_value
                if apply_points:
                    update_points(conn, chosen, point_value)
            else:
                unassigned.append({"Date": date_str, "Zone": zone, "Reason": "No eligible employees"})
        current += timedelta(days=1)

    return pd.DataFrame(schedule), pd.DataFrame(unassigned)

def reset_points(conn, names=None):
    cursor = conn.cursor()
    if names is None:
        cursor.execute("UPDATE employees SET points = 0")
    else:
        placeholders = ",".join("?" for _ in names)
        cursor.execute(f"UPDATE employees SET points = 0 WHERE name IN ({placeholders})", names)
    conn.commit()

def delete_employees(conn, names):
    if isinstance(names, str):
        names = [names]
    if not names:
        return 0
    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in names)
    cursor.execute(f"DELETE FROM employees WHERE name IN ({placeholders})", names)
    deleted = cursor.rowcount or 0
    conn.commit()
    return deleted

def export_schedule_as_matrix(schedule_df, employees_df, start_date, end_date):
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end   = datetime.strptime(end_date,   "%Y-%m-%d")
    dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range((end - start).days + 1)]
    if not employees_df.empty:
        employees = employees_df["name"].tolist()
    else:
        employees = sorted(schedule_df["Employee"].unique().tolist())
    mat = pd.DataFrame("", index=dates, columns=employees)
    for _, r in schedule_df.iterrows():
        d = str(r["Date"]); e = str(r["Employee"]); s = str(r["Shift"])
        if d in mat.index and e in mat.columns:
            cur = mat.at[d, e]
            if cur == "":
                mat.at[d, e] = s
            elif s not in cur:
                mat.at[d, e] = f"{cur}/{s}"
    # Add points row based on current schedule_df, not DB
    point_sum = schedule_df.groupby("Employee")["Points"].sum() if not schedule_df.empty else pd.Series(dtype=int)
    mat.loc["Points"] = 0
    for emp in employees:
        mat.at["Points", emp] = int(point_sum.get(emp, 0))
    # Return as Excel bytes
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        mat.to_excel(writer, sheet_name="Matrix")
        schedule_df.to_excel(writer, sheet_name="Schedule_Long", index=False)
        ws = writer.book["Matrix"]
        ws.freeze_panes = "B2"
    output.seek(0)
    return output

# ---------- UI ----------
st.title("🗓️ ABCIE Shift Scheduler")

conn = get_conn()
setup_database(conn)

with st.sidebar:
    st.header("Controls")
    st.caption("管理員工、排班與匯出")

    st.subheader("Add / Update Employee")
    with st.form("add_emp_form", clear_on_submit=False):
        name = st.text_input("Name", key="emp_name")
        initial = st.number_input("Initial Points", 0, 999, 0)
        max_pts = st.number_input("Max Points", 0, 999, 10)
        off_days = st.text_input("Off days (comma-separated YYYY-MM-DD)", help="例如 2025-08-12,2025-08-20")
        preferred = st.selectbox("Preferred Zone", ["", *ZONES])
        allowed = st.multiselect("Allowed Zones", ZONES, default=ZONES)
        submitted = st.form_submit_button("Save Employee")
        if submitted:
            off_list = [s.strip() for s in off_days.split(",") if s.strip()]
            add_or_update_employee(conn, name, int(initial), int(max_pts), off_list,
                                   preferred_zone=preferred if preferred else None,
                                   allowed_zones=allowed)
            st.success(f"Saved employee: {name}")

    st.subheader("Delete Employees")
    del_names_raw = st.text_input("Names to delete (comma-separated)", key="del_names")
    if st.button("Delete"):
        names = [s.strip() for s in del_names_raw.split(",") if s.strip()]
        count = delete_employees(conn, names)
        st.warning(f"Deleted {count} employee(s).")

    st.subheader("Reset Points")
    reset_mode = st.radio("Mode", ["All", "Selected"], horizontal=True)
    target_names_raw = st.text_input("Names (comma-separated) for Selected mode", key="reset_names")
    if st.button("Reset Points Now"):
        if reset_mode == "All":
            reset_points(conn, None)
            st.info("All points reset to 0.")
        else:
            names = [s.strip() for s in target_names_raw.split(",") if s.strip()]
            if names:
                reset_points(conn, names)
                st.info(f"Reset points for: {', '.join(names)}")
            else:
                st.error("Please enter at least one name.")

st.subheader("👥 Employees")
emp_df = get_employees(conn)
st.dataframe(emp_df)

with st.expander("📦 Seed demo employees", expanded=False):
    demo = st.button("Insert demo dataset")
    if demo:
        demo_employees = [
            {"name": "PGY1-A", "initial": 0, "max": 10, "off": ["2025-08-12", "2025-08-20"], "pref": "A", "allow": ["A","B","C"]},
            {"name": "PGY1-B", "initial": 0, "max": 10, "off": [], "pref": "B", "allow": ["A","B","C"]},
            {"name": "PGY1-C", "initial": 0, "max": 10, "off": ["2025-08-25"], "pref": "C", "allow": ["A","B","C"]},
            {"name": "R1-A", "initial": 0, "max": 9, "off": [], "pref": "A", "allow": ["A","B","C"]},
            {"name": "R1-B", "initial": 0, "max": 9, "off": [], "pref": "B", "allow": ["A","B","C"]},
            {"name": "R1-C", "initial": 0, "max": 9, "off": [], "pref": "C", "allow": ["A", "B", "C"]},
            {"name": "R2-A", "initial": 0, "max": 8, "off": [], "pref": "A", "allow": ["A","B","C","I"]},
            {"name": "R2-B", "initial": 0, "max": 8, "off": [], "pref": "B", "allow": ["A","B","C","I"]},
            {"name": "R2-C", "initial": 0, "max": 9, "off": [], "pref": "C", "allow": ["A", "B", "C","I"]},
            {"name": "R3-A", "initial": 0, "max": 7, "off": [], "pref": "I", "allow": ["A","B","C","I","E"]},
            {"name": "R3-B", "initial": 0, "max": 7, "off": [], "pref": "I", "allow": ["A", "B", "C", "I", "E"]},
            {"name": "R4-A", "initial": 0, "max": 6, "off": [], "pref": "I", "allow": ["A","B","C","I","E"]},
            {"name": "R4-B", "initial": 0, "max": 6, "off": [], "pref": "I", "allow": ["A","B","C","I","E"]},
            {"name": "R5-A", "initial": 0, "max": 5, "off": [], "pref": "E", "allow": ["A","B","C","I","E"]},
            {"name": "R5-B", "initial": 0, "max": 5, "off": [], "pref": "E", "allow": ["A", "B", "C", "I", "E"]},
            {"name": "R5-C", "initial": 0, "max": 5, "off": [], "pref": "", "allow": ["A", "B", "C", "I", "E"]},
        ]
        for emp in demo_employees:
            add_or_update_employee(
                conn,
                emp["name"], emp["initial"], emp["max"], emp["off"],
                preferred_zone=emp["pref"] if emp["pref"] else None,
                allowed_zones=emp["allow"]
            )
        st.success("Demo employees inserted. Refresh the Employees table if needed.")

st.divider()

st.subheader("🧮 Generate Schedule")
col1, col2, col3 = st.columns(3)
with col1:
    start_date = st.date_input("Start date", datetime(2025, 8, 1))
with col2:
    end_date   = st.date_input("End date", datetime(2025, 8, 7))
with col3:
    apply_points = st.checkbox("Apply points to DB while generating", value=False,
                               help="若勾選，系統會即時把本次排班產生的點數累加到資料庫。")

if st.button("Generate"):
    if start_date > end_date:
        st.error("Start date must be on or before end date.")
    else:
        df_schedule, unassigned = generate_schedule(conn, start_date.strftime("%Y-%m-%d"),
                                                    end_date.strftime("%Y-%m-%d"),
                                                    apply_points=apply_points)
        st.success("Schedule generated.")
        st.write("### 排班表（長表）")
        st.dataframe(df_schedule)
        st.write("### 未能排班")
        st.dataframe(unassigned)

        # Export section
        st.write("### 匯出 Excel")
        employees_df = get_employees(conn)
        excel_data = export_schedule_as_matrix(df_schedule, employees_df,
                                               start_date.strftime("%Y-%m-%d"),
                                               end_date.strftime("%Y-%m-%d"))
        st.download_button(
            label="Download ABCIE_shift_matrix.xlsx",
            data=excel_data,
            file_name="ABCIE_shift_matrix.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

st.divider()
st.subheader("📊 Current Points & Limits")
pts_df = get_employees(conn)[["name", "points", "max_points"]].sort_values("name")
st.dataframe(pts_df)

st.caption("© ABCIE Scheduler – Streamlit adaptation")
