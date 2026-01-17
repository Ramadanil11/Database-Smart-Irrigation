import os
from datetime import datetime, time, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv
import logging

load_dotenv()

app = FastAPI(title="Smart Irrigation API v4")

# ========== LOGGING ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== CORS MIDDLEWARE ==========
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== TIMEZONE ==========
TIMEZONE_OFFSET = 7

def get_local_time():
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET)

# ========== MODELS ==========
class SensorData(BaseModel):
    moisture_level: float
    water_level: float

class ScheduleData(BaseModel):
    on_time: str  # "HH:MM:SS"
    off_time: str  # "HH:MM:SS"

class ControlUpdate(BaseModel):
    action: str  # "MANUAL_ON", "MANUAL_OFF", "AUTO", "PAUSE"
    minutes: Optional[int] = None

# ========== DATABASE ==========
def get_db():
    """Get database connection with retry logic"""
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
            logger.info(f"✅ DB connected (attempt {attempt + 1})")
            return db
        except Error as e:
            logger.error(f"❌ DB Error (attempt {attempt + 1}): {e}")
            if attempt == max_retries - 1:
                return None
            
def init_db():
    """Initialize database tables if they don't exist"""
    db = get_db()
    if not db:
        logger.error("❌ Cannot initialize DB")
        return
    
    try:
        cursor = db.cursor()
        
        # Create pump_control table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pump_control (
                id INT PRIMARY KEY DEFAULT 1,
                manual_mode VARCHAR(20) DEFAULT 'AUTO',
                pause_end_time DATETIME NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)
        
        # Create pump_schedules table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pump_schedules (
                id INT AUTO_INCREMENT PRIMARY KEY,
                on_time TIME NOT NULL,
                off_time TIME NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)
        
        # Create sensor_data table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sensor_data (
                id INT AUTO_INCREMENT PRIMARY KEY,
                moisture_level FLOAT NOT NULL,
                water_level FLOAT NOT NULL,
                pump_status VARCHAR(10) DEFAULT 'OFF',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Insert default pump_control if not exists
        cursor.execute("SELECT COUNT(*) FROM pump_control WHERE id = 1")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT INTO pump_control (id, manual_mode, pause_end_time)
                VALUES (1, 'AUTO', NULL)
            """)
        
        cursor.close()
        logger.info("✅ Database initialized")
    except Error as e:
        logger.error(f"❌ Init DB Error: {e}")
    finally:
        db.close()

# ========== HELPER FUNCTIONS ==========

def parse_time(time_str: str) -> time:
    """Parse HH:MM:SS to time object"""
    try:
        parts = time_str.split(':')
        return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
    except Exception as e:
        logger.error(f"❌ Error parsing time {time_str}: {e}")
        return time(0, 0, 0)

def is_in_schedule(now_dt: datetime, on_str: str, off_str: str) -> bool:
    """Check if current time is within schedule"""
    try:
        on_t = parse_time(on_str)
        off_t = parse_time(off_str)
        now_t = now_dt.time()
        
        if on_t <= off_t:
            result = on_t <= now_t <= off_t
        else:
            result = now_t >= on_t or now_t <= off_t
        
        logger.info(f"  Schedule check: {now_t} in {on_t}-{off_t} = {result}")
        return result
    except Exception as e:
        logger.error(f"❌ Error checking schedule: {e}")
        return False

def calculate_pump_status(db, now_dt: datetime) -> str:
    """Calculate final pump status based on priority"""
    logger.info(f"\n{'='*60}")
    logger.info(f"🔄 CALCULATING PUMP STATUS at {now_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'='*60}")
    
    try:
        cursor = db.cursor(dictionary=True)
        
        # Get current control state
        cursor.execute("SELECT * FROM pump_control WHERE id = 1")
        control = cursor.fetchone()
        
        if not control:
            logger.warning("⚠️ No control record found!")
            cursor.close()
            return "OFF"
        
        logger.info(f"\n📊 Control State:")
        logger.info(f"   manual_mode: {control['manual_mode']}")
        logger.info(f"   pause_end_time: {control['pause_end_time']}")
        
        # PRIORITY 1: Check if PAUSE is active
        if control['pause_end_time']:
            pause_dt = control['pause_end_time']
            logger.info(f"\n⏸️  [PRIORITY 1] PAUSE CHECK:")
            logger.info(f"   Pause until: {pause_dt}")
            logger.info(f"   Now: {now_dt}")
            
            if now_dt < pause_dt:
                logger.info(f"   ✓ PAUSE ACTIVE → Return OFF")
                cursor.close()
                return "OFF"
            else:
                logger.info(f"   ✓ Pause expired, clearing...")
                cursor.execute("""
                    UPDATE pump_control SET pause_end_time = NULL WHERE id = 1
                """)
        
        # PRIORITY 2: Check MANUAL mode
        logger.info(f"\n🔧 [PRIORITY 2] MANUAL MODE CHECK:")
        logger.info(f"   manual_mode: {control['manual_mode']}")
        
        if control['manual_mode'] == 'MANUAL_ON':
            logger.info(f"   ✓ MANUAL_ON → Return ON")
            cursor.close()
            return "ON"
        elif control['manual_mode'] == 'MANUAL_OFF':
            logger.info(f"   ✓ MANUAL_OFF → Return OFF")
            cursor.close()
            return "OFF"
        
        # PRIORITY 3: Check AUTO (Schedule)
        logger.info(f"\n📅 [PRIORITY 3] AUTO MODE (SCHEDULE) CHECK:")
        cursor.execute("""
            SELECT on_time, off_time FROM pump_schedules 
            WHERE is_active = TRUE LIMIT 1
        """)
        schedule = cursor.fetchone()
        
        if schedule:
            on_time = schedule['on_time']
            off_time = schedule['off_time']
            
            if hasattr(on_time, 'strftime'):
                on_time = on_time.strftime('%H:%M:%S')
            if hasattr(off_time, 'strftime'):
                off_time = off_time.strftime('%H:%M:%S')
            
            logger.info(f"   Schedule found: {on_time} - {off_time}")
            
            if is_in_schedule(now_dt, on_time, off_time):
                logger.info(f"   ✓ Within schedule → Return ON")
                cursor.close()
                return "ON"
            else:
                logger.info(f"   ✓ Outside schedule → Return OFF")
                cursor.close()
                return "OFF"
        else:
            logger.info(f"   No schedule found → Return OFF")
            cursor.close()
            return "OFF"
    
    except Error as e:
        logger.error(f"❌ Error calculating status: {e}")
        cursor.close()
        return "OFF"

# ========== ENDPOINTS ==========

@app.on_event("startup")
async def startup():
    """Initialize database on startup"""
    init_db()

@app.get("/")
async def root():
    return {
        "status": "online",
        "version": "4.0",
        "timestamp": get_local_time().isoformat()
    }

@app.get("/health")
async def health():
    db = get_db()
    if db:
        db.close()
        return {
            "status": "healthy",
            "database": "connected",
            "server_time": get_local_time().isoformat(),
            "timezone": f"UTC+{TIMEZONE_OFFSET}"
        }
    return {
        "status": "unhealthy",
        "database": "disconnected"
    }, 500

@app.get("/api/sensor/latest")
async def get_latest():
    """Get latest sensor data"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT moisture_level, water_level, pump_status, created_at 
            FROM sensor_data ORDER BY created_at DESC LIMIT 1
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
            "moisture_level": 0.0,
            "water_level": 0.0,
            "pump_status": "OFF",
            "created_at": None
        }
    except Error as e:
        logger.error(f"❌ Get latest error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/sensor/history")
async def get_history(limit: int = 100):
    """Get sensor history for chart"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT moisture_level, water_level, pump_status, created_at 
            FROM sensor_data 
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
            ORDER BY created_at ASC LIMIT %s
        """, (limit,))
        rows = cursor.fetchall()
        cursor.close()
        
        return [{
            "moisture": float(h['moisture_level']),
            "water": float(h['water_level']),
            "pump_status": h['pump_status'],
            "time": h['created_at'].strftime("%H:%M") if h['created_at'] else ""
        } for h in rows]
    except Error as e:
        logger.error(f"❌ Get history error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/sensor/save")
async def save_sensor(data: SensorData):
    """Save sensor data and return pump command"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        now = get_local_time()
        
        logger.info(f"\n📨 SENSOR DATA RECEIVED")
        logger.info(f"   Moisture: {data.moisture_level}%")
        logger.info(f"   Water: {data.water_level}%")
        
        pump_status = calculate_pump_status(db, now)
        
        cursor = db.cursor()
        cursor.execute("""
            INSERT INTO sensor_data (moisture_level, water_level, pump_status)
            VALUES (%s, %s, %s)
        """, (data.moisture_level, data.water_level, pump_status))
        cursor.close()
        
        logger.info(f"💾 Saved to DB: pump_status = {pump_status}")
        logger.info(f"{'='*60}\n")
        
        return {
            "status": "success",
            "command": pump_status,
            "timestamp": now.isoformat()
        }
    except Error as e:
        logger.error(f"❌ Save sensor error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/control/update")
async def update_control(update: ControlUpdate):
    """Update pump control"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        action = update.action.upper()
        now = get_local_time()
        
        logger.info(f"\n{'='*60}")
        logger.info(f"🎮 CONTROL UPDATE: {action}")
        logger.info(f"{'='*60}")
        
        cursor = db.cursor()
        
        if action == "PAUSE":
            minutes = update.minutes or 30
            pause_until = now + timedelta(minutes=minutes)
            
            logger.info(f"⏸️  Pausing for {minutes} minutes")
            logger.info(f"   Until: {pause_until.strftime('%Y-%m-%d %H:%M:%S')}")
            
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'AUTO', pause_end_time = %s 
                WHERE id = 1
            """, (pause_until,))
            
            result_msg = f"Pause set for {minutes} minutes"
        
        elif action == "MANUAL_ON":
            logger.info(f"🔌 Setting MANUAL_ON")
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'MANUAL_ON', pause_end_time = NULL 
                WHERE id = 1
            """)
            result_msg = "Manual ON"
        
        elif action == "MANUAL_OFF":
            logger.info(f"🔌 Setting MANUAL_OFF")
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'MANUAL_OFF', pause_end_time = NULL 
                WHERE id = 1
            """)
            result_msg = "Manual OFF"
        
        elif action == "AUTO":
            logger.info(f"📅 Setting AUTO mode")
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'AUTO', pause_end_time = NULL 
                WHERE id = 1
            """)
            result_msg = "Auto mode"
        
        else:
            cursor.close()
            raise HTTPException(status_code=400, detail="Invalid action")
        
        cursor.close()
        
        logger.info(f"✅ {result_msg}")
        logger.info(f"{'='*60}\n")
        
        return {
            "status": "success",
            "action": action,
            "message": result_msg,
            "timestamp": now.isoformat()
        }
    
    except Error as e:
        logger.error(f"❌ Update control error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/schedule/add")
async def add_schedule(data: ScheduleData):
    """Add or update schedule"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        logger.info(f"\n{'='*60}")
        logger.info(f"📅 SCHEDULE UPDATE")
        logger.info(f"   ON: {data.on_time}")
        logger.info(f"   OFF: {data.off_time}")
        
        cursor = db.cursor()
        
        cursor.execute("DELETE FROM pump_schedules")
        
        cursor.execute("""
            INSERT INTO pump_schedules (on_time, off_time, is_active)
            VALUES (%s, %s, TRUE)
        """, (data.on_time, data.off_time))
        
        cursor.close()
        
        logger.info(f"✅ Schedule saved")
        logger.info(f"{'='*60}\n")
        
        return {
            "status": "success",
            "on_time": data.on_time,
            "off_time": data.off_time
        }
    
    except Error as e:
        logger.error(f"❌ Add schedule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/schedule/list")
async def get_schedule():
    """Get current schedule"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT on_time, off_time, is_active 
            FROM pump_schedules WHERE is_active = TRUE LIMIT 1
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
        
        return {
            "on_time": None,
            "off_time": None,
            "is_active": False
        }
    except Error as e:
        logger.error(f"❌ Get schedule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/control/status")
async def get_control_status():
    """Get current control state"""
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
        
        return {
            "manual_mode": control['manual_mode'] if control else "AUTO",
            "pause_end_time": control['pause_end_time'].isoformat() if control and control['pause_end_time'] else None,
            "calculated_pump_status": calculated_status,
            "server_time": now.isoformat()
        }
    except Error as e:
        logger.error(f"❌ Get control status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()