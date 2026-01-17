import os
from datetime import datetime, time, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
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

# --- TIMEZONE CONFIGURATION ---
TIMEZONE_OFFSET = 7  # WIB (UTC+7) - Ganti sesuai timezone lokal Anda

def get_local_time():
    """Get current time dengan timezone offset"""
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET)

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
    """Connect to Railway MySQL"""
    try:
        db_host = os.getenv('MYSQL_HOST')
        db_user = os.getenv('MYSQL_USER')
        db_pass = os.getenv('MYSQL_PASSWORD')
        db_name = os.getenv('MYSQL_DATABASE')
        db_port = int(os.getenv('MYSQL_PORT', 3306))

        conn = mysql.connector.connect(
            host=db_host,
            user=db_user,
            password=db_pass,
            database=db_name,
            port=db_port,
            autocommit=True,
            connect_timeout=10
        )
        return conn
    except mysql.connector.Error as err:
        print(f"❌ Database Error: {err}")
        return None

# --- HELPERS ---
def parse_time_str(t: str) -> time:
    """Parse HH:MM or HH:MM:SS to time object"""
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
    """Check if current time is between on_time and off_time"""
    try:
        on_t = parse_time_str(on_str)
        off_t = parse_time_str(off_str)
    except Exception as e:
        print(f"Error parsing time: {e}")
        return False
    
    now_t = now_dt.time()
    
    # Debug log
    print(f"Checking: {now_t} between {on_t} and {off_t}")
    
    if on_t <= off_t:
        # Normal case: 07:00 to 18:00
        return on_t <= now_t <= off_t
    else:
        # Overnight case: 22:00 to 06:00
        return now_t >= on_t or now_t <= off_t

# --- ENDPOINTS ---

@app.get("/")
async def root():
    return {"status": "online", "message": "Smart Irrigation API - FastAPI"}

@app.get("/health")
async def health():
    """Health check dengan current time"""
    db = get_db_connection()
    current_time = get_local_time()
    
    if db:
        db.close()
        return {
            "status": "healthy",
            "database": "connected",
            "server_time": current_time.isoformat(),
            "timezone": f"UTC+{TIMEZONE_OFFSET}"
        }
    return {
        "status": "unhealthy",
        "database": "disconnected",
        "server_time": current_time.isoformat()
    }

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
async def get_history(limit: int = 20):
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
            "moisture": float(h['moisture_level']),
            "water": float(h['water_level']),
            "pump_status": h['pump_status'],
            "time": h['created_at'].strftime("%H:%M") if h['created_at'] else "N/A"
        } for h in rows]
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/sensor/add")
async def add_sensor_data(data: SensorData):
    """Add sensor data manually"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO sensor_data (moisture_level, water_level, pump_status)
            VALUES (%s, %s, %s)
        """, (data.moisture_level, data.water_level, data.pump_status))
        cursor.close()
        
        return {"status": "data added"}
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/sensor/save")
async def save_sensor_data(data: SensorData):
    """Save sensor data from ESP32 with auto pump logic"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        # Gunakan local time (dengan timezone offset)
        now = get_local_time()
        target_status = "OFF"
        
        print(f"\n=== Sensor Save Called ===")
        print(f"Current time: {now.strftime('%H:%M:%S')}")
        print(f"Moisture: {data.moisture_level}, Water: {data.water_level}")
        
        cursor = db.cursor(dictionary=True)
        
        # Check pump control (pause & manual)
        cursor.execute("""
            SELECT manual_target, pause_until FROM pump_control 
            ORDER BY id DESC LIMIT 1
        """)
        ctrl = cursor.fetchone()
        
        print(f"Control: {ctrl}")
        
        if ctrl:
            # Priority 1: Check if pause is active
            if ctrl['pause_until']:
                pause_dt = ctrl['pause_until']
                print(f"Pause until: {pause_dt}")
                
                if now < pause_dt:
                    print("✓ Pause is active - pump OFF")
                    target_status = "OFF"
                else:
                    print("✓ Pause expired - check schedule")
                    # Pause expired, clear it
                    cursor.execute("""
                        UPDATE pump_control SET pause_until = NULL 
                        WHERE id = %s
                    """, (ctrl['id'],))
            
            # Priority 2: Check manual control (only if pause not active)
            if target_status != "OFF" or not ctrl['pause_until']:
                if ctrl['manual_target'] == 'ON':
                    print("✓ Manual ON - pump ON")
                    target_status = "ON"
        
        # Priority 3: Check schedule (only if no manual ON)
        if target_status != "ON":
            cursor.execute("""
                SELECT on_time, off_time FROM pump_schedules 
                WHERE is_active = TRUE LIMIT 1
            """)
            sched = cursor.fetchone()
            
            if sched:
                print(f"Schedule found: {sched['on_time']} to {sched['off_time']}")
                if is_now_between(sched['on_time'], sched['off_time'], now):
                    print("✓ Within schedule - pump ON")
                    target_status = "ON"
                else:
                    print("✗ Outside schedule - pump OFF")
                    target_status = "OFF"
            else:
                print("No schedule found")
                target_status = "OFF"
        
        # Save sensor data
        cursor.execute("""
            INSERT INTO sensor_data (moisture_level, water_level, pump_status)
            VALUES (%s, %s, %s)
        """, (data.moisture_level, data.water_level, target_status))
        
        print(f"Pump status: {target_status}\n")
        
        cursor.close()
        return {"status": "success", "command": target_status}
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/control/update")
async def update_control(control: ControlUpdate):
    """Update pump control - manual ON/OFF or pause"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        
        # Get last control record
        cursor.execute("SELECT id FROM pump_control ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        
        if not row:
            cursor.execute("INSERT INTO pump_control (manual_target) VALUES ('OFF')")
            last_id = cursor.lastrowid
        else:
            last_id = row['id']

        if control.type == "manual":
            # Manual control - clear pause
            target = (control.target.upper() if control.target else "OFF")
            print(f"Manual control: {target}")
            
            cursor.execute("""
                UPDATE pump_control 
                SET manual_target = %s, pause_until = NULL 
                WHERE id = %s
            """, (target, last_id))
        
        elif control.type == "pause":
            # Pause control - gunakan local time
            minutes = control.minutes or 0
            pause_until = get_local_time() + timedelta(minutes=minutes)
            
            print(f"Pause set: {minutes} minutes until {pause_until.strftime('%H:%M:%S')}")
            
            cursor.execute("""
                UPDATE pump_control 
                SET pause_until = %s, manual_target = 'OFF' 
                WHERE id = %s
            """, (pause_until, last_id))
        
        cursor.close()
        return {"status": "success"}
    except Exception as e:
        print(f"❌ Error update_control: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/schedule/add")
async def add_schedule(schedule: ScheduleData):
    """Add pump schedule"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor()
        
        print(f"Adding schedule: {schedule.on_time} to {schedule.off_time}")
        
        # Clear old schedules
        cursor.execute("DELETE FROM pump_schedules")
        
        # Insert new schedule
        cursor.execute("""
            INSERT INTO pump_schedules (on_time, off_time, is_active)
            VALUES (%s, %s, TRUE)
        """, (schedule.on_time, schedule.off_time))
        
        cursor.close()
        return {"status": "schedule added"}
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/schedule/list")
async def get_schedules():
    """Get active schedules"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, on_time, off_time, is_active 
            FROM pump_schedules 
            WHERE is_active = TRUE
        """)
        schedules = cursor.fetchall()
        cursor.close()
        return schedules if schedules else []
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.delete("/api/schedule/{schedule_id}")
async def delete_schedule(schedule_id: int):
    """Delete schedule"""
    db = get_db_connection()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor()
        cursor.execute("UPDATE pump_schedules SET is_active = FALSE WHERE id = %s", (schedule_id,))
        cursor.close()
        return {"status": "schedule deleted"}
    except Exception as e:
        print(f"Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()