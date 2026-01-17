import os
from datetime import datetime, time, timedelta
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Smart Irrigation API")

# --- CORS MIDDLEWARE ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- PYDANTIC MODELS ---
class SensorData(BaseModel):
    moisture_level: float
    water_level: float
    pump_status: str = "OFF"

class ScheduleData(BaseModel):
    on_time: str
    off_time: str

class ControlUpdate(BaseModel):
    type: str
    target: Optional[str] = None
    minutes: Optional[int] = None

def get_db_connection():
    """Connect to Railway MySQL using standard Environment Variables"""
    try:
        db_host = os.getenv('MYSQLHOST')
        db_user = os.getenv('MYSQLUSER')
        db_pass = os.getenv('MYSQLPASSWORD')
        db_name = os.getenv('MYSQLDATABASE')
        db_port = os.getenv('MYSQLPORT', 3306)

        if not db_host:
            print("❌ Error: Variabel MYSQLHOST tidak ditemukan!")
            return None

        conn = mysql.connector.connect(
            host=db_host,
            user=db_user,
            password=db_pass,
            database=db_name,
            port=int(db_port),
            autocommit=True,
            connect_timeout=10
        )
        return conn
    except mysql.connector.Error as err:
        print(f"❌ Database Error: {err}")
        return None

# --- HELPERS ---
def parse_time_str(t: str) -> time:
    """Parse HH:MM or HH:MM:SS"""
    if not t:
        raise ValueError("empty time")
    parts = t.split(":")
    parts = [int(p) for p in parts]
    if len(parts) == 2:
        h, m = parts
        s = 0
    elif len(parts) == 3:
        h, m, s = parts
    else:
        raise ValueError("invalid time format")
    return time(h, m, s)

def is_now_between(on_str: str, off_str: str, now_dt: datetime) -> bool:
    """Check if now is between on_time and off_time (handles overnight)"""
    try:
        on_t = parse_time_str(on_str)
        off_t = parse_time_str(off_str)
    except Exception:
        return False
    
    now_t = now_dt.time()
    
    if on_t <= off_t:
        return on_t <= now_t <= off_t
    else:
        # overnight range (e.g., 22:00 to 06:00)
        return now_t >= on_t or now_t <= off_t

# --- ENDPOINTS ---

@app.get("/")
async def root():
    return {"status": "online", "message": "Smart Irrigation API - FastAPI"}

@app.get("/health")
async def health():
    """Health check"""
    db = get_db_connection()
    if db:
        db.close()
        return {"status": "healthy", "database": "connected"}
    return {"status": "unhealthy", "database": "disconnected"}

@app.get("/api/sensor/latest")
async def get_latest():
    """Get latest sensor data"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT moisture_level, water_level, pump_status, created_at 
            FROM sensor_data 
            ORDER BY created_at DESC LIMIT 1
        """)
        row = cursor.fetchone()
        cursor.close()
        
        if row:
            return {
                "moisture_level": float(row['moisture_level']),
                "water_level": float(row['water_level']),
                "pump_status": row['pump_status'],
                "created_at": row['created_at'].isoformat() if row['created_at'] else None
            }
        return {
            "moisture_level": 0,
            "water_level": 0,
            "pump_status": "OFF",
            "created_at": None
        }
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/sensor/history")
async def get_history(limit: int = 7):
    """Get sensor history for charts"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT moisture_level, water_level, pump_status, created_at 
            FROM sensor_data 
            ORDER BY created_at DESC LIMIT %s
        """, (limit,))
        rows = cursor.fetchall()
        cursor.close()
        
        if not rows:
            return []
        
        rows.reverse()
        return [{
            "moisture": h['moisture_level'],
            "water": h['water_level'],
            "pump_status": h['pump_status'],
            "time": h['created_at'].strftime("%H:%M") if h['created_at'] else "N/A"
        } for h in rows]
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/sensor/save")
async def save_sensor_data(data: SensorData):
    """Save sensor data from ESP32 with smart pump logic"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        now = datetime.utcnow() + timedelta(hours=7) # Sesuaikan ke WIB jika perlu
        target_status = "OFF"
        
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT manual_target, pause_until FROM pump_control ORDER BY id DESC LIMIT 1")
        ctrl = cursor.fetchone()
        
        if ctrl:
            if ctrl['manual_target'] == 'ON':
                target_status = "ON"
            
            if ctrl['pause_until'] and now < ctrl['pause_until']:
                target_status = "OFF"
            else:
                if ctrl['pause_until']:
                    cursor.execute("UPDATE pump_control SET pause_until = NULL")
        
        if target_status != "ON":
            cursor.execute("SELECT on_time, off_time FROM pump_schedules WHERE is_active = TRUE")
            schedules = cursor.fetchall()
            for sched in schedules:
                if is_now_between(sched['on_time'], sched['off_time'], now):
                    target_status = "ON"
                    break
        
        cursor.execute("""
            INSERT INTO sensor_data (moisture_level, water_level, pump_status)
            VALUES (%s, %s, %s)
        """, (data.moisture_level, data.water_level, target_status))
        
        return {"status": "success", "command": target_status}
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/control/update")
async def update_control(control: ControlUpdate):
    """Update pump control - FIXED for MySQL 1093 error and consistency"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        
        # Ambil ID terakhir dulu untuk menghindari error subquery
        cursor.execute("SELECT id FROM pump_control ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        
        if not row:
            # Jika tabel kosong, masukkan data pertama
            cursor.execute("INSERT INTO pump_control (manual_target) VALUES ('OFF')")
            last_id = cursor.lastrowid
        else:
            last_id = row['id']

        if control.type == "manual":
            target = control.target.upper() if control.target else "OFF"
            cursor.execute("""
                UPDATE pump_control 
                SET manual_target = %s, pause_until = NULL 
                WHERE id = %s
            """, (target, last_id))
        
        elif control.type == "pause":
            # Waktu sekarang + durasi (Gunakan UTC agar konsisten dengan server)
            pause_until = datetime.utcnow() + timedelta(minutes=control.minutes or 0)
            cursor.execute("""
                UPDATE pump_control 
                SET pause_until = %s, manual_target = 'OFF' 
                WHERE id = %s
            """, (pause_until, last_id))
        
        return {"status": "success"}
    except Exception as e:
        print(f"❌ Error update_control: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/schedule/add")
async def add_schedule(schedule: ScheduleData):
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    try:
        cursor = db.cursor()
        cursor.execute("UPDATE pump_schedules SET is_active = FALSE")
        cursor.execute("""
            INSERT INTO pump_schedules (on_time, off_time, is_active)
            VALUES (%s, %s, TRUE)
        """, (schedule.on_time, schedule.off_time))
        return {"status": "schedule added"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/schedule/list")
async def get_schedules():
    db = get_db_connection()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT id, on_time, off_time, is_active FROM pump_schedules WHERE is_active = TRUE")
        return cursor.fetchall()
    finally:
        db.close()

@app.delete("/api/schedule/{schedule_id}")
async def delete_schedule(schedule_id: int):
    db = get_db_connection()
    try:
        cursor = db.cursor()
        cursor.execute("UPDATE pump_schedules SET is_active = FALSE WHERE id = %s", (schedule_id,))
        return {"status": "schedule deleted"}
    finally:
        db.close()