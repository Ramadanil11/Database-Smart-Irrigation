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
import pytz

load_dotenv()

app = FastAPI(title="Smart Irrigation API v8 - Fixed")

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

# Timezone Indonesia (WIB = UTC+7)
TIMEZONE = pytz.timezone('Asia/Jakarta')

def get_local_time():
    """Get current time in WIB timezone"""
    return datetime.now(TIMEZONE)

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
        
        # Table pump_control - FIXED SCHEMA
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pump_control (
                id INT PRIMARY KEY DEFAULT 1,
                manual_mode VARCHAR(20) DEFAULT 'AUTO',
                manual_target VARCHAR(10) DEFAULT 'OFF',
                pause_until DATETIME NULL,
                pause_end_time DATETIME NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)
        
        # Table pump_schedules
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pump_schedules (
                id INT AUTO_INCREMENT PRIMARY KEY,
                on_time TIME NOT NULL,
                off_time TIME NOT NULL,
                is_active BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Table sensor_data
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sensor_data (
                id INT AUTO_INCREMENT PRIMARY KEY,
                moisture_level FLOAT NOT NULL,
                water_level FLOAT NOT NULL,
                pump_status VARCHAR(10) DEFAULT 'OFF',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Control logs
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS control_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                action VARCHAR(50) NOT NULL,
                source VARCHAR(50) DEFAULT 'API',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Initialize pump_control
        cursor.execute("SELECT COUNT(*) FROM pump_control WHERE id = 1")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT INTO pump_control (id, manual_mode, manual_target, pause_until, pause_end_time)
                VALUES (1, 'AUTO', 'OFF', NULL, NULL)
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
    """Check if current time is within schedule"""
    try:
        on_t = parse_time(on_str)
        off_t = parse_time(off_str)
        now_t = now_dt.time()
        
        if on_t <= off_t:
            # Normal case: 07:00 - 18:00
            result = on_t <= now_t <= off_t
        else:
            # Overnight case: 20:00 - 06:00
            result = now_t >= on_t or now_t <= off_t
        
        logger.info(f"📅 Schedule check: {on_t} to {off_t}, now={now_t}, result={result}")
        return result
    except Exception as e:
        logger.error(f"❌ Schedule check error: {e}")
        return False

def calculate_pump_status(db, now_dt: datetime) -> str:
    """
    FIXED: Proper pump status calculation with correct priority
    Priority: 1. PAUSE > 2. MANUAL > 3. SCHEDULE
    """
    try:
        cursor = db.cursor(dictionary=True)
        
        cursor.execute("SELECT * FROM pump_control WHERE id = 1")
        control = cursor.fetchone()
        
        if not control:
            cursor.close()
            return "OFF"
        
        # PRIORITY 1: Check PAUSE
        pause_end = control.get('pause_end_time') or control.get('pause_until')
        
        if pause_end:
            # Convert to datetime if needed
            if isinstance(pause_end, str):
                pause_dt = datetime.strptime(pause_end, '%Y-%m-%d %H:%M:%S')
                pause_dt = TIMEZONE.localize(pause_dt)
            else:
                pause_dt = pause_end
                if pause_dt.tzinfo is None:
                    pause_dt = TIMEZONE.localize(pause_dt)
            
            # Make now_dt timezone aware
            if now_dt.tzinfo is None:
                now_dt = TIMEZONE.localize(now_dt)
            
            if now_dt < pause_dt:
                logger.info(f"⏸️ PAUSE active until {pause_dt}")
                cursor.close()
                return "OFF"
            else:
                # PAUSE EXPIRED - Clear it and continue to next priority
                logger.info(f"✅ Pause expired, clearing...")
                cursor.execute("""
                    UPDATE pump_control 
                    SET pause_end_time = NULL, pause_until = NULL 
                    WHERE id = 1
                """)
                # Log the action
                cursor.execute("""
                    INSERT INTO control_logs (action, source) 
                    VALUES ('PAUSE_EXPIRED', 'SYSTEM')
                """)
        
        # PRIORITY 2: Check MANUAL mode
        manual_mode = control.get('manual_mode', 'AUTO')
        
        if manual_mode == 'MANUAL':
            manual_target = control.get('manual_target', 'OFF')
            logger.info(f"🔌 MANUAL mode: target={manual_target}")
            cursor.close()
            return manual_target
        
        # PRIORITY 3: Check SCHEDULE (AUTO mode)
        cursor.execute("""
            SELECT on_time, off_time FROM pump_schedules 
            WHERE is_active = TRUE 
            ORDER BY id DESC LIMIT 1
        """)
        schedule = cursor.fetchone()
        
        if schedule:
            on_time = schedule['on_time']
            off_time = schedule['off_time']
            
            # Convert timedelta to string if needed
            if hasattr(on_time, 'total_seconds'):
                hours = int(on_time.total_seconds() // 3600)
                minutes = int((on_time.total_seconds() % 3600) // 60)
                on_time = f"{hours:02d}:{minutes:02d}:00"
            elif hasattr(on_time, 'strftime'):
                on_time = on_time.strftime('%H:%M:%S')
            
            if hasattr(off_time, 'total_seconds'):
                hours = int(off_time.total_seconds() // 3600)
                minutes = int((off_time.total_seconds() % 3600) // 60)
                off_time = f"{hours:02d}:{minutes:02d}:00"
            elif hasattr(off_time, 'strftime'):
                off_time = off_time.strftime('%H:%M:%S')
            
            if is_in_schedule(now_dt, on_time, off_time):
                logger.info(f"✅ Within schedule → ON")
                cursor.close()
                return "ON"
            else:
                logger.info(f"❌ Outside schedule → OFF")
                cursor.close()
                return "OFF"
        
        logger.info(f"📅 No active schedule → OFF")
        cursor.close()
        return "OFF"
    
    except Exception as e:
        logger.error(f"❌ Calculate pump status error: {e}")
        if cursor:
            cursor.close()
        return "OFF"

@app.on_event("startup")
async def startup():
    migrate_db()

@app.get("/")
async def root():
    return {"status": "online", "version": "8.0-fixed"}

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
                "last_update": row['created_at'].isoformat() if row['created_at'] else None
            }
        return {
            "moisture_level": 0.0, 
            "water_level": 0.0, 
            "pump_status": "OFF",
            "last_update": None
        }
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
            SELECT moisture_level, water_level, created_at 
            FROM sensor_data 
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
            ORDER BY created_at ASC LIMIT %s
        """, (limit,))
        rows = cursor.fetchall()
        cursor.close()
        
        return [{
            "moisture": float(h['moisture_level']),
            "water": float(h['water_level']),
            "timestamp": h['created_at'].isoformat() if h['created_at'] else None
        } for h in rows]
    finally:
        db.close()

@app.post("/api/sensor/save")
async def save_sensor(data: SensorData):
    """
    FIXED: Save sensor data with proper pump status calculation
    """
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        now = get_local_time()
        
        # Calculate pump status based on all rules
        pump_status = calculate_pump_status(db, now)
        
        cursor = db.cursor()
        
        # Save sensor data with calculated pump status
        cursor.execute("""
            INSERT INTO sensor_data (moisture_level, water_level, pump_status, created_at)
            VALUES (%s, %s, %s, %s)
        """, (data.moisture_level, data.water_level, pump_status, now))
        
        cursor.close()
        
        logger.info(f"💾 Saved: moisture={data.moisture_level:.1f}%, water={data.water_level:.1f}%, pump={pump_status}")
        
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
    """
    FIXED: Control update with proper database updates
    """
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
            
            # FIXED: Update both pause_until AND pause_end_time
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'AUTO', 
                    manual_target = 'OFF',
                    pause_until = %s,
                    pause_end_time = %s
                WHERE id = 1
            """, (pause_until, pause_until))
            
            cursor.execute("""
                INSERT INTO control_logs (action, source) 
                VALUES (%s, 'APP')
            """, (f"PAUSE_{minutes}MIN",))
            
            logger.info(f"⏸️ Pause for {minutes} minutes until {pause_until}")
            msg = f"Pause set for {minutes} minutes"
        
        elif action == "MANUAL_ON":
            # FIXED: Clear pause and set manual mode properly
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'MANUAL',
                    manual_target = 'ON',
                    pause_until = NULL,
                    pause_end_time = NULL
                WHERE id = 1
            """)
            
            cursor.execute("""
                INSERT INTO control_logs (action, source) 
                VALUES ('MANUAL_ON', 'APP')
            """)
            
            logger.info(f"🔌 MANUAL_ON")
            msg = "Manual ON"
        
        elif action == "MANUAL_OFF":
            # FIXED: Clear pause and set manual mode properly
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'MANUAL',
                    manual_target = 'OFF',
                    pause_until = NULL,
                    pause_end_time = NULL
                WHERE id = 1
            """)
            
            cursor.execute("""
                INSERT INTO control_logs (action, source) 
                VALUES ('MANUAL_OFF', 'APP')
            """)
            
            logger.info(f"🔌 MANUAL_OFF")
            msg = "Manual OFF"
        
        elif action == "AUTO":
            # FIXED: Clear all manual settings and pause
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'AUTO',
                    manual_target = 'OFF',
                    pause_until = NULL,
                    pause_end_time = NULL
                WHERE id = 1
            """)
            
            cursor.execute("""
                INSERT INTO control_logs (action, source) 
                VALUES ('AUTO_MODE', 'APP')
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
    """
    FIXED: Schedule with proper is_active handling
    """
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        logger.info(f"📅 Schedule: {data.on_time} - {data.off_time}")
        cursor = db.cursor()
        
        # Set all existing schedules to inactive
        cursor.execute("UPDATE pump_schedules SET is_active = FALSE")
        
        # Insert new schedule (will be activated when time comes)
        cursor.execute("""
            INSERT INTO pump_schedules (on_time, off_time, is_active)
            VALUES (%s, %s, FALSE)
        """, (data.on_time, data.off_time))
        
        # Log the action
        cursor.execute("""
            INSERT INTO control_logs (action, source) 
            VALUES (%s, 'APP')
        """, (f"SCHEDULE_{data.on_time}_{data.off_time}",))
        
        # IMPORTANT: Activate schedule immediately if we're within the time range
        now = get_local_time()
        if is_in_schedule(now, data.on_time, data.off_time):
            cursor.execute("""
                UPDATE pump_schedules 
                SET is_active = TRUE 
                WHERE on_time = %s AND off_time = %s
            """, (data.on_time, data.off_time))
            logger.info(f"✅ Schedule activated immediately (within time range)")
        else:
            # Activate for future use
            cursor.execute("""
                UPDATE pump_schedules 
                SET is_active = TRUE 
                WHERE on_time = %s AND off_time = %s
            """, (data.on_time, data.off_time))
            logger.info(f"✅ Schedule will activate at {data.on_time}")
        
        cursor.close()
        
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
            SELECT on_time, off_time, is_active FROM pump_schedules 
            WHERE is_active = TRUE LIMIT 1
        """)
        row = cursor.fetchone()
        cursor.close()
        
        if row:
            on_time = row['on_time']
            off_time = row['off_time']
            
            # Convert timedelta to string if needed
            if hasattr(on_time, 'total_seconds'):
                hours = int(on_time.total_seconds() // 3600)
                minutes = int((on_time.total_seconds() % 3600) // 60)
                on_time = f"{hours:02d}:{minutes:02d}:00"
            elif hasattr(on_time, 'strftime'):
                on_time = on_time.strftime('%H:%M:%S')
            
            if hasattr(off_time, 'total_seconds'):
                hours = int(off_time.total_seconds() // 3600)
                minutes = int((off_time.total_seconds() % 3600) // 60)
                off_time = f"{hours:02d}:{minutes:02d}:00"
            elif hasattr(off_time, 'strftime'):
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
    """
    Get current control status including pump, pause, and schedule info
    """
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        now = get_local_time()
        
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT * FROM pump_control WHERE id = 1")
        control = cursor.fetchone()
        
        calculated_status = calculate_pump_status(db, now) if control else "OFF"
        
        cursor.close()
        
        pause_end_time = None
        if control:
            pause_end = control.get('pause_end_time') or control.get('pause_until')
            if pause_end:
                pause_end_time = pause_end.isoformat() if hasattr(pause_end, 'isoformat') else str(pause_end)
        
        return {
            "calculated_pump_status": calculated_status,
            "manual_mode": control['manual_mode'] if control else 'AUTO',
            "manual_target": control['manual_target'] if control else 'OFF',
            "pause_end_time": pause_end_time,
            "server_time": now.isoformat()
        }
    except Error as e:
        logger.error(f"❌ Status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/device/heartbeat")
async def device_heartbeat():
    """
    Endpoint for ESP32 to report it's online
    Returns current pump command
    """
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        now = get_local_time()
        pump_status = calculate_pump_status(db, now)
        
        return {
            "status": "success",
            "command": pump_status,
            "timestamp": now.isoformat()
        }
    finally:
        db.close()