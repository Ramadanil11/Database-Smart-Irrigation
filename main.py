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
import asyncio
from contextlib import asynccontextmanager

load_dotenv()

# ========== LOGGING ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== TIMEZONE ==========
TIMEZONE_OFFSET = 7

def get_local_time():
    """Get current time in WIB (UTC+7)"""
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET)

# ========== MODELS ==========
class SensorData(BaseModel):
    moisture_level: float
    water_level: float

class ScheduleData(BaseModel):
    on_time: str
    off_time: str

class ControlUpdate(BaseModel):
    action: str
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
            return db
        except Error as e:
            logger.error(f"❌ DB Error (attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                asyncio.sleep(1)
            else:
                return None

def migrate_db():
    """Initialize database schema"""
    db = get_db()
    if not db:
        logger.error("❌ Cannot connect to database for migration")
        return
    
    try:
        cursor = db.cursor()
        
        # Pump control table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pump_control (
                id INT PRIMARY KEY DEFAULT 1,
                manual_target VARCHAR(20) DEFAULT 'AUTO',
                pause_end_time DATETIME NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        """)
        
        # Pump schedules table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pump_schedules (
                id INT AUTO_INCREMENT PRIMARY KEY,
                on_time TIME NOT NULL,
                off_time TIME NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Sensor data table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sensor_data (
                id INT AUTO_INCREMENT PRIMARY KEY,
                moisture_level FLOAT NOT NULL,
                water_level FLOAT NOT NULL,
                pump_status VARCHAR(10) DEFAULT 'OFF',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_created_at (created_at)
            )
        """)
        
        # Insert default control row if not exists
        cursor.execute("SELECT COUNT(*) FROM pump_control WHERE id = 1")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT INTO pump_control (id, manual_target, pause_end_time)
                VALUES (1, 'AUTO', NULL)
            """)
            logger.info("✅ Default control row inserted")
        
        cursor.close()
        logger.info("✅ Database initialized successfully")
    except Error as e:
        logger.error(f"❌ Migration error: {e}")
    finally:
        db.close()

# ========== HELPER FUNCTIONS ==========
def parse_time(time_str: str) -> time:
    """Parse time string to time object"""
    try:
        parts = time_str.split(':')
        return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
    except Exception as e:
        logger.error(f"❌ Time parse error: {e}")
        return time(0, 0, 0)

def is_in_schedule(now_dt: datetime, on_str: str, off_str: str) -> bool:
    """Check if current time is within schedule"""
    try:
        on_t = parse_time(on_str)
        off_t = parse_time(off_str)
        now_t = now_dt.time()
        
        if on_t <= off_t:
            # Normal case: 08:00 - 18:00
            return on_t <= now_t <= off_t
        else:
            # Overnight case: 22:00 - 06:00
            return now_t >= on_t or now_t <= off_t
    except Exception as e:
        logger.error(f"❌ Schedule check error: {e}")
        return False

def calculate_pump_status(db, now_dt: datetime) -> str:
    """
    Calculate pump status based on priority:
    1. PAUSE (highest priority)
    2. MANUAL (ON/OFF)
    3. SCHEDULE (AUTO mode)
    """
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
            if isinstance(pause_end_time, str):
                pause_dt = datetime.fromisoformat(pause_end_time)
            else:
                pause_dt = pause_end_time
            
            if now_dt < pause_dt:
                logger.info(f"⏸️ Pause active until {pause_dt} → FORCE OFF")
                cursor.close()
                return "OFF"
            else:
                # Clear expired pause
                logger.info(f"✅ Pause expired, clearing...")
                cursor.execute("UPDATE pump_control SET pause_end_time = NULL WHERE id = 1")
                
                # Refresh control data
                cursor.execute("SELECT * FROM pump_control WHERE id = 1")
                control = cursor.fetchone()
        
        # PRIORITY 2: Check MANUAL_TARGET
        manual_target = control.get('manual_target', 'AUTO')
        
        if manual_target == 'ON':
            logger.info(f"🔌 MANUAL ON active")
            cursor.close()
            return "ON"
        elif manual_target == 'OFF':
            logger.info(f"🔌 MANUAL OFF active")
            cursor.close()
            return "OFF"
        
        # PRIORITY 3: Check SCHEDULE (AUTO mode)
        if manual_target == 'AUTO':
            cursor.execute("""
                SELECT on_time, off_time FROM pump_schedules 
                WHERE is_active = TRUE ORDER BY id DESC LIMIT 1
            """)
            schedule = cursor.fetchone()
            
            if schedule:
                on_time = schedule['on_time']
                off_time = schedule['off_time']
                
                # Convert to string if needed
                if hasattr(on_time, 'strftime'):
                    on_time = on_time.strftime('%H:%M:%S')
                if hasattr(off_time, 'strftime'):
                    off_time = off_time.strftime('%H:%M:%S')
                
                if is_in_schedule(now_dt, on_time, off_time):
                    logger.info(f"✅ Within schedule {on_time}-{off_time} → ON")
                    cursor.close()
                    return "ON"
                else:
                    logger.info(f"❌ Outside schedule {on_time}-{off_time} → OFF")
                    cursor.close()
                    return "OFF"
            
            logger.info(f"📅 No active schedule → OFF")
            cursor.close()
            return "OFF"
        
        cursor.close()
        return "OFF"
    
    except Exception as e:
        logger.error(f"❌ Error in calculate_pump_status: {e}")
        if 'cursor' in locals():
            cursor.close()
        return "OFF"

# ========== BACKGROUND TASKS ==========
async def auto_check_pause_expiry():
    """Background task to check and clear expired pauses"""
    while True:
        try:
            await asyncio.sleep(10)
            
            db = get_db()
            if not db:
                continue
            
            try:
                now = get_local_time()
                cursor = db.cursor(dictionary=True)
                
                cursor.execute("SELECT pause_end_time FROM pump_control WHERE id = 1")
                control = cursor.fetchone()
                
                if control and control.get('pause_end_time'):
                    pause_end_time = control['pause_end_time']
                    
                    if isinstance(pause_end_time, str):
                        pause_dt = datetime.fromisoformat(pause_end_time)
                    else:
                        pause_dt = pause_end_time
                    
                    if now >= pause_dt:
                        logger.info(f"🔄 Auto-clearing expired pause")
                        cursor.execute("UPDATE pump_control SET pause_end_time = NULL WHERE id = 1")
                
                cursor.close()
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Auto-check error: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage app lifecycle"""
    # Startup
    migrate_db()
    task = asyncio.create_task(auto_check_pause_expiry())
    logger.info("✅ System started - v9.0-PRODUCTION-FIXED")
    
    yield
    
    # Shutdown
    task.cancel()
    logger.info("🛑 System shutting down")

# ========== FASTAPI APP ==========
app = FastAPI(
    title="Smart Irrigation API v9.0-FIXED",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== ENDPOINTS ==========
@app.get("/")
async def root():
    return {
        "status": "online", 
        "version": "9.0-PRODUCTION-FIXED", 
        "timezone": "WIB (UTC+7)",
        "features": [
            "wifi_reconnect",
            "error_handling",
            "pause_auto_clear",
            "manual_target_priority",
            "1_day_history"
        ]
    }

@app.get("/health")
async def health():
    db = get_db()
    if db:
        db.close()
        return {"status": "healthy", "database": "connected"}
    return {"status": "unhealthy", "database": "disconnected"}

@app.get("/api/sensor/latest")
async def get_latest():
    """Get latest sensor reading"""
    db = get_db()
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
                "last_update": row['created_at'].isoformat() if row['created_at'] else None
            }
        return {
            "moisture_level": 0.0, 
            "water_level": 0.0, 
            "pump_status": "OFF", 
            "last_update": None
        }
    except Error as e:
        logger.error(f"❌ Get latest error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/sensor/history")
async def get_history(limit: int = 1000):
    """Get sensor history for the last 24 hours"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        
        # Get last 24 hours of data
        cursor.execute("""
            SELECT 
                moisture_level as moisture,
                water_level as water,
                created_at,
                pump_status
            FROM sensor_data 
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL 1 DAY)
            ORDER BY created_at ASC
            LIMIT %s
        """, (limit,))
        
        rows = cursor.fetchall()
        logger.info(f"📊 History query returned {len(rows)} rows")
        
        cursor.close()
        
        result = [{
            "moisture": float(h['moisture']),
            "water": float(h['water']),
            "timestamp": h['created_at'].isoformat() if hasattr(h['created_at'], 'isoformat') else str(h['created_at'])
        } for h in rows]
        
        return result
    except Error as e:
        logger.error(f"❌ History error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/sensor/save")
async def save_sensor(data: SensorData):
    """Save sensor data and return pump command"""
    db = get_db()
    if not db:
        logger.error("❌ Database connection failed")
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        now = get_local_time()
        
        # Validate and clamp data
        moisture = max(0, min(100, data.moisture_level))
        water = max(0, min(100, data.water_level))
        
        # Calculate pump status
        pump_status = calculate_pump_status(db, now)
        
        cursor = db.cursor()
        
        try:
            cursor.execute("""
                INSERT INTO sensor_data (moisture_level, water_level, pump_status, created_at)
                VALUES (%s, %s, %s, %s)
            """, (moisture, water, pump_status, now))
            
            insert_id = cursor.lastrowid
            logger.info(f"💾 ID={insert_id}: M={moisture:.1f}%, W={water:.1f}%, Pump={pump_status}")
            
        except Error as insert_error:
            logger.error(f"❌ Insert error: {insert_error}")
            cursor.close()
            db.close()
            raise HTTPException(status_code=500, detail=f"Insert failed: {str(insert_error)}")
        
        cursor.close()
        
        return {
            "status": "success",
            "command": pump_status,
            "timestamp": now.isoformat(),
            "data_saved": {
                "moisture": moisture,
                "water": water
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Save sensor error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if db:
            db.close()

@app.post("/api/control/update")
async def update_control(update: ControlUpdate):
    """Update pump control (PAUSE, MANUAL_ON, MANUAL_OFF, AUTO)"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        action = update.action.upper()
        now = get_local_time()
        
        logger.info(f"🎮 Control action: {action}")
        cursor = db.cursor(dictionary=True)
        
        if action == "PAUSE":
            minutes = update.minutes or 30
            pause_until = now + timedelta(minutes=minutes)
            
            cursor.execute("""
                UPDATE pump_control 
                SET pause_end_time = %s 
                WHERE id = 1
            """, (pause_until,))
            
            logger.info(f"⏸️ Pause set for {minutes} minutes until {pause_until}")
            msg = f"Pause set for {minutes} minutes"
        
        elif action == "MANUAL_ON":
            cursor.execute("""
                UPDATE pump_control 
                SET manual_target = 'ON', pause_end_time = NULL 
                WHERE id = 1
            """)
            logger.info(f"🔌 Manual ON activated")
            msg = "Manual ON activated"
        
        elif action == "MANUAL_OFF":
            cursor.execute("""
                UPDATE pump_control 
                SET manual_target = 'OFF', pause_end_time = NULL 
                WHERE id = 1
            """)
            logger.info(f"🔌 Manual OFF activated")
            msg = "Manual OFF activated"
        
        elif action == "AUTO":
            cursor.execute("""
                UPDATE pump_control 
                SET manual_target = 'AUTO', pause_end_time = NULL 
                WHERE id = 1
            """)
            logger.info(f"📅 Auto mode activated")
            msg = "Auto mode activated"
        
        else:
            cursor.close()
            raise HTTPException(status_code=400, detail="Invalid action")
        
        cursor.close()
        
        # Get new pump status
        new_status = calculate_pump_status(db, now)
        logger.info(f"📊 New pump status: {new_status}")
        
        return {
            "status": "success",
            "action": action,
            "message": msg,
            "pump_status": new_status
        }
    except HTTPException:
        raise
    except Error as e:
        logger.error(f"❌ Control error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.post("/api/schedule/add")
async def add_schedule(data: ScheduleData):
    """Add/update pump schedule"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        logger.info(f"📅 Adding schedule: {data.on_time} - {data.off_time}")
        cursor = db.cursor()
        
        # Deactivate all schedules
        cursor.execute("UPDATE pump_schedules SET is_active = FALSE")
        
        # Add new schedule
        cursor.execute("""
            INSERT INTO pump_schedules (on_time, off_time, is_active)
            VALUES (%s, %s, TRUE)
        """, (data.on_time, data.off_time))
        
        # Set to AUTO mode and clear pause
        cursor.execute("""
            UPDATE pump_control 
            SET manual_target = 'AUTO', pause_end_time = NULL 
            WHERE id = 1
        """)
        
        cursor.close()
        logger.info(f"✅ Schedule saved, system in AUTO mode")
        
        return {
            "status": "success",
            "on_time": data.on_time,
            "off_time": data.off_time,
            "message": "Schedule saved, system in AUTO mode"
        }
    except Error as e:
        logger.error(f"❌ Schedule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/schedule/list")
async def get_schedule():
    """Get active schedule"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT on_time, off_time FROM pump_schedules 
            WHERE is_active = TRUE ORDER BY id DESC LIMIT 1
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
        logger.error(f"❌ Schedule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.delete("/api/schedule/delete")
async def delete_schedule():
    """Delete all schedules"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        logger.info(f"🗑️ Deleting all schedules")
        cursor = db.cursor()
        
        cursor.execute("DELETE FROM pump_schedules")
        deleted_count = cursor.rowcount
        
        cursor.execute("ALTER TABLE pump_schedules AUTO_INCREMENT = 1")
        
        # Keep AUTO mode, just remove schedules
        cursor.execute("""
            UPDATE pump_control 
            SET manual_target = 'AUTO', pause_end_time = NULL 
            WHERE id = 1
        """)
        
        cursor.close()
        logger.info(f"✅ {deleted_count} schedule(s) deleted")
        
        return {
            "status": "success",
            "message": f"{deleted_count} schedule(s) deleted",
            "deleted_count": deleted_count
        }
    except Error as e:
        logger.error(f"❌ Delete error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/control/status")
async def get_control_status():
    """Get current control status"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="Database offline")
    
    try:
        now = get_local_time()
        
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT * FROM pump_control WHERE id = 1")
        control = cursor.fetchone()
        
        calculated_status = calculate_pump_status(db, now)
        
        cursor.close()
        
        pause_end_time = None
        manual_target = "AUTO"
        
        if control:
            if control.get('pause_end_time'):
                pause_end_time = control['pause_end_time'].isoformat() if hasattr(control['pause_end_time'], 'isoformat') else str(control['pause_end_time'])
            manual_target = control.get('manual_target', 'AUTO')
        
        return {
            "calculated_pump_status": calculated_status,
            "manual_target": manual_target,
            "pause_end_time": pause_end_time,
            "server_time": now.isoformat()
        }
    except Error as e:
        logger.error(f"❌ Status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)