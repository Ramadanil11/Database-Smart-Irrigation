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

load_dotenv()

app = FastAPI(title="Smart Irrigation API v8.4-FIXED")

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
            logger.error(f"❌ DB Error (attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                asyncio.sleep(1)
            else:
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
    """
    FIXED: Proper pump status calculation
    - Saat pause selesai, sistem kembali ke AUTO mode TANPA otomatis ON
    - Hanya ON jika ada schedule aktif yang match waktu saat ini
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
                # Pause masih aktif
                logger.info(f"⏸️ Pause active until {pause_dt}")
                cursor.close()
                return "OFF"
            else:
                # FIX: Pause sudah selesai - clear pause DAN reset ke AUTO mode
                # TAPI JANGAN langsung ON, harus cek schedule dulu
                logger.info(f"✅ Pause expired at {pause_dt}, clearing pause...")
                cursor.execute("""
                    UPDATE pump_control 
                    SET pause_end_time = NULL, manual_mode = 'AUTO' 
                    WHERE id = 1
                """)
                
                # Refresh control data setelah update
                cursor.execute("SELECT * FROM pump_control WHERE id = 1")
                control = cursor.fetchone()
        
        # PRIORITY 2: Check MANUAL mode
        manual_mode = control.get('manual_mode', 'AUTO')
        
        if manual_mode == 'MANUAL_ON':
            logger.info(f"🔌 MANUAL_ON active")
            cursor.close()
            return "ON"
        elif manual_mode == 'MANUAL_OFF':
            logger.info(f"🔌 MANUAL_OFF active")
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
            
            logger.info(f"📅 Checking schedule: {on_time} - {off_time} (mode: {manual_mode})")
            
            if is_in_schedule(now_dt, on_time, off_time):
                logger.info(f"✅ Within schedule → ON")
                cursor.close()
                return "ON"
            else:
                logger.info(f"❌ Outside schedule → OFF")
                cursor.close()
                return "OFF"
        
        # FIX: Tidak ada schedule aktif = OFF (bukan ON)
        logger.info(f"📅 No active schedule → OFF")
        cursor.close()
        return "OFF"
    
    except Exception as e:
        logger.error(f"❌ Error in calculate_pump_status: {e}")
        if 'cursor' in locals():
            cursor.close()
        return "OFF"

async def auto_check_pause_expiry():
    """Background task to check pause expiry every 10 seconds"""
    while True:
        try:
            await asyncio.sleep(10)
            
            db = get_db()
            if not db:
                continue
            
            try:
                now = get_local_time()
                cursor = db.cursor(dictionary=True)
                
                cursor.execute("SELECT pause_end_time, manual_mode FROM pump_control WHERE id = 1")
                control = cursor.fetchone()
                
                if control and control.get('pause_end_time'):
                    pause_end_time = control['pause_end_time']
                    
                    if isinstance(pause_end_time, str):
                        pause_dt = datetime.fromisoformat(pause_end_time)
                    else:
                        pause_dt = pause_end_time
                    
                    if now >= pause_dt:
                        logger.info(f"🔄 Auto-check: Pause expired, updating status")
                        
                        # FIX: Hitung status dengan benar (tidak otomatis ON)
                        new_status = calculate_pump_status(db, now)
                        
                        # Insert sensor data dengan status yang benar
                        cursor.execute("""
                            INSERT INTO sensor_data (moisture_level, water_level, pump_status)
                            SELECT 
                                COALESCE((SELECT moisture_level FROM sensor_data ORDER BY created_at DESC LIMIT 1), 0),
                                COALESCE((SELECT water_level FROM sensor_data ORDER BY created_at DESC LIMIT 1), 0),
                                %s
                        """, (new_status,))
                        
                        logger.info(f"✅ Auto-check: Pause cleared, pump status = {new_status}")
                
                cursor.close()
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Auto-check error: {e}")

@app.on_event("startup")
async def startup():
    migrate_db()
    asyncio.create_task(auto_check_pause_expiry())
    logger.info("✅ Auto-check pause expiry task started")

@app.get("/")
async def root():
    return {
        "status": "online", 
        "version": "8.4-FIXED", 
        "fixes": ["sensor_data_update", "pause_auto_on_bug"]
    }

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
        return {"moisture_level": 0.0, "water_level": 0.0, "pump_status": "OFF", "last_update": None}
    except Error as e:
        logger.error(f"❌ Get latest error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.get("/api/sensor/history")
async def get_history(limit: int = 1000):
    """Get sensor history for the last 2 days"""
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        
        cursor.execute("SELECT COUNT(*) as total FROM sensor_data")
        total = cursor.fetchone()['total']
        logger.info(f"📊 Total records in sensor_data: {total}")
        
        cursor.execute("""
            SELECT 
                moisture_level as moisture,
                water_level as water,
                created_at,
                pump_status
            FROM sensor_data 
            WHERE created_at >= DATE_SUB(
                (SELECT MAX(created_at) FROM sensor_data), 
                INTERVAL 2 DAY
            )
            ORDER BY created_at ASC
            LIMIT %s
        """, (limit,))
        
        rows = cursor.fetchall()
        logger.info(f"📊 Query returned {len(rows)} rows from last 2 days")
        
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
    """
    FIX: Improved sensor data saving with better error handling
    """
    db = get_db()
    if not db:
        logger.error("❌ Database connection failed in save_sensor")
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        now = get_local_time()
        
        # Hitung pump status
        pump_status = calculate_pump_status(db, now)
        
        # Validate data
        if data.moisture_level < 0 or data.moisture_level > 100:
            logger.warning(f"⚠️ Invalid moisture_level: {data.moisture_level}")
            data.moisture_level = max(0, min(100, data.moisture_level))
        
        if data.water_level < 0 or data.water_level > 100:
            logger.warning(f"⚠️ Invalid water_level: {data.water_level}")
            data.water_level = max(0, min(100, data.water_level))
        
        cursor = db.cursor()
        
        # FIX: Explicit insert with error handling
        try:
            cursor.execute("""
                INSERT INTO sensor_data (moisture_level, water_level, pump_status, created_at)
                VALUES (%s, %s, %s, %s)
            """, (data.moisture_level, data.water_level, pump_status, now))
            
            # Verify insert
            insert_id = cursor.lastrowid
            logger.info(f"💾 Saved ID={insert_id}: moisture={data.moisture_level}%, water={data.water_level}%, pump={pump_status}")
            
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
                "moisture": data.moisture_level,
                "water": data.water_level
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
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        action = update.action.upper()
        now = get_local_time()
        
        logger.info(f"🎮 CONTROL ACTION: {action}")
        cursor = db.cursor()
        
        if action == "PAUSE":
            minutes = update.minutes or 30
            pause_until = now + timedelta(minutes=minutes)
            
            # FIX: Set ke AUTO mode saat pause (bukan MANUAL_OFF)
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'AUTO', pause_end_time = %s 
                WHERE id = 1
            """, (pause_until,))
            
            logger.info(f"⏸️ Pause for {minutes} minutes until {pause_until}, mode=AUTO")
            msg = f"Pause set for {minutes} minutes"
        
        elif action == "MANUAL_ON":
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'MANUAL_ON', pause_end_time = NULL 
                WHERE id = 1
            """)
            logger.info(f"🔌 MANUAL_ON activated")
            msg = "Manual ON activated"
        
        elif action == "MANUAL_OFF":
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'MANUAL_OFF', pause_end_time = NULL 
                WHERE id = 1
            """)
            logger.info(f"🔌 MANUAL_OFF activated")
            msg = "Manual OFF activated"
        
        elif action == "AUTO":
            cursor.execute("""
                UPDATE pump_control 
                SET manual_mode = 'AUTO', pause_end_time = NULL 
                WHERE id = 1
            """)
            logger.info(f"📅 AUTO mode activated")
            msg = "Auto mode activated"
        
        else:
            cursor.close()
            raise HTTPException(status_code=400, detail="Invalid action")
        
        cursor.close()
        
        new_status = calculate_pump_status(db, now)
        logger.info(f"📊 New pump status: {new_status}")
        
        return {
            "status": "success",
            "action": action,
            "message": msg,
            "pump_status": new_status
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
        logger.info(f"📅 Adding schedule: {data.on_time} - {data.off_time}")
        cursor = db.cursor()
        
        cursor.execute("UPDATE pump_schedules SET is_active = FALSE")
        
        cursor.execute("""
            INSERT INTO pump_schedules (on_time, off_time, is_active)
            VALUES (%s, %s, TRUE)
        """, (data.on_time, data.off_time))
        
        cursor.execute("""
            UPDATE pump_control 
            SET manual_mode = 'AUTO', pause_end_time = NULL 
            WHERE id = 1
        """)
        
        cursor.close()
        logger.info(f"✅ Schedule saved and system reset to AUTO mode")
        
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
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
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
        
        return {"on_time": None, "off_time": None, "is_active": False}
    except Error as e:
        logger.error(f"❌ Schedule error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()

@app.delete("/api/schedule/delete")
async def delete_schedule():
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        logger.info(f"🗑️ Deleting ALL schedules from database")
        cursor = db.cursor()
        
        cursor.execute("DELETE FROM pump_schedules")
        deleted_count = cursor.rowcount
        
        cursor.execute("ALTER TABLE pump_schedules AUTO_INCREMENT = 1")
        
        cursor.execute("""
            UPDATE pump_control 
            SET manual_mode = 'AUTO', pause_end_time = NULL 
            WHERE id = 1
        """)
        
        cursor.close()
        logger.info(f"✅ {deleted_count} schedule(s) deleted, system reset to AUTO")
        
        return {
            "status": "success",
            "message": f"{deleted_count} schedule(s) deleted successfully",
            "deleted_count": deleted_count,
            "mode": "AUTO"
        }
    except Error as e:
        logger.error(f"❌ Delete schedule error: {e}")
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
        
        calculated_status = calculate_pump_status(db, now)
        
        cursor.close()
        
        pause_end_time = None
        manual_mode = "AUTO"
        
        if control:
            if control.get('pause_end_time'):
                pause_end_time = control['pause_end_time'].isoformat() if hasattr(control['pause_end_time'], 'isoformat') else str(control['pause_end_time'])
            manual_mode = control.get('manual_mode', 'AUTO')
        
        return {
            "calculated_pump_status": calculated_status,
            "manual_mode": manual_mode,
            "pause_end_time": pause_end_time,
            "server_time": now.isoformat()
        }
    except Error as e:
        logger.error(f"❌ Status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()