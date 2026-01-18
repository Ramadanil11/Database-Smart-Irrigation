import os
from datetime import datetime, time, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv
import logging

load_dotenv()

app = FastAPI(title="Smart Irrigation API v7")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TIMEZONE_OFFSET = 7

def get_local_time():
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET)

class SensorData(BaseModel):
    moisture_level: float
    water_level: float

class ScheduleData(BaseModel):
    on_time: str
    off_time: str

class ControlUpdate(BaseModel):
    action: str
    minutes: Optional[int] = None

def get_db():
    max_retries = 3
    for attempt in range(max_retries):
        try:
            db = mysql.connector.connect(
                host=os.getenv('MYSQLHOST'),
                user=os.getenv('MYSQLUSER'),
                password=os.getenv('MYSQLPASSWORD'),
                database=os.getenv('MYSQLDATABASE'),
                port=int(os.getenv('MYSQLPORT', 3306)),
                autocommit=True,
                connection_timeout=10
            )
            return db
        except Error as e:
            logger.error(f"❌ DB Error: {e}")
            if attempt == max_retries - 1:
                return None

def migrate_db():
    db = get_db()
    if not db:
        return
    
    try:
        cursor = db.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pump_control (
                id INT PRIMARY KEY DEFAULT 1,
                manual_mode VARCHAR(20) DEFAULT 'AUTO',
                pause_end_time DATETIME NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pump_schedules (
                id INT AUTO_INCREMENT PRIMARY KEY,
                on_time TIME NOT NULL,
                off_time TIME NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sensor_data (
                id INT AUTO_INCREMENT PRIMARY KEY,
                moisture_level FLOAT NOT NULL,
                water_level FLOAT NOT NULL,
                pump_status VARCHAR(10) DEFAULT 'OFF',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("SELECT COUNT(*) FROM pump_control WHERE id = 1")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT INTO pump_control (id, manual_mode, pause_end_time)
                VALUES (1, 'AUTO', NULL)
            """)
        
        cursor.close()
        logger.info("✅ Database initialized")
    except Error as e:
        logger.error(f"❌ Migration error: {e}")
    finally:
        db.close()

def parse_time(time_str: str) -> time:
    try:
        parts = time_str.split(':')
        return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
    except:
        return time(0, 0, 0)

def is_in_schedule(now_dt: datetime, on_str: str, off_str: str) -> bool:
    try:
        on_t = parse_time(on_str)
        off_t = parse_time(off_str)
        now_t = now_dt.time()
        
        if on_t <= off_t:
            result = on_t <= now_t <= off_t
        else:
            result = now_t >= on_t or now_t <= off_t
        
        return result
    except:
        return False

def calculate_pump_status(db, now_dt: datetime) -> str:
    """FIXED: Proper pump status calculation"""
    try:
        cursor = db.cursor(dictionary=True)
        
        cursor.execute("SELECT * FROM pump_control WHERE id = 1")
        control = cursor.fetchone()
        
        if not control:
            cursor.close()
            return "OFF"
        
        # PRIORITY 1: Check PAUSE
        pause_end_time = control.get('pause_end_time')
        if pause_end_time:
            # Convert to datetime if needed
            if isinstance(pause_end_time, str):
                pause_dt = datetime.fromisoformat(pause_end_time)
            else:
                pause_dt = pause_end_time
            
            if now_dt < pause_dt:
                # Pause masih aktif
                logger.info(f"⏸️ Pause active until {pause_dt}")
                cursor.close()
                return "OFF"
            else:
                # Pause expired - clear it
                logger.info(f"✅ Pause expired, clearing")
                cursor.execute("UPDATE pump_control SET pause_end_time = NULL WHERE id = 1")
                # Continue to next priority
        
        # PRIORITY 2: Check MANUAL mode
        manual_mode = control.get('manual_mode', 'AUTO')
        
        if manual_mode == 'MANUAL_ON':
            logger.info(f"🔌 MANUAL_ON")
            cursor.close()
            return "ON"
        elif manual_mode == 'MANUAL_OFF':
            logger.info(f"🔌 MANUAL_OFF")
            cursor.close()
            return "OFF"
        
        # PRIORITY 3: Check SCHEDULE (AUTO mode)
        cursor.execute("""
            SELECT on_time, off_time FROM pump_schedules 
            WHERE is_active = TRUE ORDER BY id DESC LIMIT 1
        """)
        schedule = cursor.fetchone()
        
        if schedule:
            on_time = schedule['on_time']
            off_time = schedule['off_time']
            
            if hasattr(on_time, 'strftime'):
                on_time = on_time.strftime('%H:%M:%S')
            if hasattr(off_time, 'strftime'):
                off_time = off_time.strftime('%H:%M:%S')
            
            logger.info(f"📅 Checking schedule: {on_time} - {off_time}")
            
            if is_in_schedule(now_dt, on_time, off_time):
                logger.info(f"✅ Within schedule → ON")
                cursor.close()
                return "ON"
            else:
                logger.info(f"❌ Outside schedule → OFF")
                cursor.close()
                return "OFF"
        
        logger.info(f"📅 No schedule found → OFF")
        cursor.close()
        return "OFF"
    
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        cursor.close()
        return "OFF"

@app.on_event("startup")
async def startup():
    migrate_db()

@app.get("/")
async def root():
    return {"status": "online", "version": "7.0"}

@app.get("/health")
async def health():
    db = get_db()
    if db:
        db.close()
        return {"status": "healthy", "database": "connected"}
    return {"status": "unhealthy"}

@app.get("/api/sensor/latest")
async def get_latest():
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT moisture_level, water_level, pump_status FROM sensor_data 
            ORDER BY created_at DESC LIMIT 1
        """)
        row = cursor.fetchone()
        cursor.close()
        
        if row:
            return {
                "moisture_level": float(row['moisture_level']),
                "water_level": float(row['water_level']),
                "pump_status": row['pump_status']
            }
        return {"moisture_level": 0.0, "water_level": 0.0, "pump_status": "OFF"}
    finally:
        db.close()

@app.get("/api/sensor/history")
async def get_history(limit: int = 100):
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT moisture_level, water_level FROM sensor_data 
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
            ORDER BY created_at ASC LIMIT %s
        """, (limit,))
        rows = cursor.fetchall()
        cursor.close()
        
        return [{
            "moisture": float(h['moisture_level']),
            "water": float(h['water_level'])
        } for h in rows]
    finally:
        db.close()

@app.post("/api/sensor/save")
async def save_sensor(data: SensorData):
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        now = get_local_time()
        pump_status = calculate_pump_status(db, now)
        
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO sensor_data (moisture_level, water_level, pump_status)
            VALUES (%s, %s, %s)
        """, (data.moisture_level, data.water_level, pump_status))
        cursor.close()
        
        logger.info(f"💾 Saved: moisture={data.moisture_level}%, water={data.water_level}%, pump={pump_status}")
        
        return {
            "status": "success",
            "command": pump_status,
            "timestamp": now.isoformat()
        }
    except Error as e:
        logger.error(f"❌ Save error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/control/update")
async def update_control(update: ControlUpdate):
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        action = update.action.upper()
        now = get_local_time()
        
        logger.info(f"🎮 CONTROL: {action}")
        cursor = db.cursor()
        
        if action == "PAUSE":
            minutes = update.minutes or 30
            pause_until = now + timedelta(minutes=minutes)
            
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'AUTO', pause_end_time = %s 
                WHERE id = 1
            """, (pause_until,))
            
            logger.info(f"⏸️ Pause for {minutes} minutes until {pause_until}")
            msg = f"Pause set for {minutes} minutes"
        
        elif action == "MANUAL_ON":
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'MANUAL_ON', pause_end_time = NULL 
                WHERE id = 1
            """)
            logger.info(f"🔌 MANUAL_ON")
            msg = "Manual ON"
        
        elif action == "MANUAL_OFF":
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'MANUAL_OFF', pause_end_time = NULL 
                WHERE id = 1
            """)
            logger.info(f"🔌 MANUAL_OFF")
            msg = "Manual OFF"
        
        elif action == "AUTO":
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'AUTO', pause_end_time = NULL 
                WHERE id = 1
            """)
            logger.info(f"📅 AUTO mode")
            msg = "Auto mode"
        
        else:
            cursor.close()
            raise HTTPException(status_code=400, detail="Invalid action")
        
        cursor.close()
        
        return {
            "status": "success",
            "action": action,
            "message": msg
        }
    except Error as e:
        logger.error(f"❌ Control error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/schedule/add")
async def add_schedule(data: ScheduleData):
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        logger.info(f"📅 Schedule: {data.on_time} - {data.off_time}")
        cursor = db.cursor()
        
        cursor.execute("DELETE FROM pump_schedules")
        cursor.execute("""
            INSERT INTO pump_schedules (on_time, off_time, is_active)
            VALUES (%s, %s, TRUE)
        """, (data.on_time, data.off_time))
        
        cursor.close()
        logger.info(f"✅ Schedule saved")
        
        return {
            "status": "success",
            "on_time": data.on_time,
            "off_time": data.off_time
        }
    except Error as e:
        logger.error(f"❌ Schedule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/schedule/list")
async def get_schedule():
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT on_time, off_time FROM pump_schedules 
            WHERE is_active = TRUE LIMIT 1
        """)
        row = cursor.fetchone()
        cursor.close()
        
        if row:
            on_time = row['on_time']
            off_time = row['off_time']
            
            if hasattr(on_time, 'strftime'):
                on_time = on_time.strftime('%H:%M:%S')
            if hasattr(off_time, 'strftime'):
                off_time = off_time.strftime('%H:%M:%S')
            
            return {
                "on_time": on_time,
                "off_time": off_time,
                "is_active": True
            }
        
        return {"on_time": None, "off_time": None, "is_active": False}
    except Error as e:
        logger.error(f"❌ Schedule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/control/status")
async def get_control_status():
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        now = get_local_time()
        
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT * FROM pump_control WHERE id = 1")
        control = cursor.fetchone()
        
        if not control:
            calculated_status = "OFF"
        else:
            calculated_status = calculate_pump_status(db, now)
        
        cursor.close()
        
        pause_end_time = None
        if control and control.get('pause_end_time'):
            pause_end_time = control['pause_end_time'].isoformat() if hasattr(control['pause_end_time'], 'isoformat') else str(control['pause_end_time'])
        
        return {
            "calculated_pump_status": calculated_status,
            "pause_end_time": pause_end_time,
            "server_time": now.isoformat()
        }
    except Error as e:
        logger.error(f"❌ Status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()