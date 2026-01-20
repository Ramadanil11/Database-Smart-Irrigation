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

app = FastAPI(title="Smart Irrigation API v9.0-FINAL-FIX")

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
    """
    FIX: Update database schema sesuai dengan struktur yang benar
    - Column: manual_target (ON/OFF/AUTO)
    - pause_until column untuk menyimpan waktu pause
    """
    db = get_db()
    if not db:
        return
    
    try:
        cursor = db.cursor()
        
        # FIX: Struktur yang benar sesuai screenshot
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pump_control (
                id INT PRIMARY KEY DEFAULT 1,
                manual_target VARCHAR(20) DEFAULT 'AUTO',
                pause_until DATETIME NULL,
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
                INSERT INTO pump_control (id, manual_target, pause_end_time)
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
    FIXED: Logic yang benar sesuai struktur database
    - Gunakan manual_target (ON/OFF/AUTO)
    - Saat pause selesai, TIDAK reset ke AUTO
    - Priority: PAUSE > MANUAL (ON/OFF) > SCHEDULE (AUTO)
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
                # Pause masih aktif - pompa HARUS OFF
                logger.info(f"⏸️ Pause active until {pause_dt} → FORCE OFF")
                cursor.close()
                return "OFF"
            else:
                # FIX: Pause sudah selesai - clear pause TANPA mengubah manual_target
                logger.info(f"✅ Pause expired at {pause_dt}, clearing pause...")
                cursor.execute("""
                    UPDATE pump_control 
                    SET pause_end_time = NULL 
                    WHERE id = 1
                """)
                
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
                
                if hasattr(on_time, 'strftime'):
                    on_time = on_time.strftime('%H:%M:%S')
                if hasattr(off_time, 'strftime'):
                    off_time = off_time.strftime('%H:%M:%S')
                
                logger.info(f"📅 Schedule check: {on_time} - {off_time}")
                
                if is_in_schedule(now_dt, on_time, off_time):
                    logger.info(f"✅ Within schedule → ON")
                    cursor.close()
                    return "ON"
                else:
                    logger.info(f"❌ Outside schedule → OFF")
                    cursor.close()
                    return "OFF"
            
            logger.info(f"📅 No schedule → OFF")
            cursor.close()
            return "OFF"
        
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
                
                cursor.execute("SELECT pause_end_time, manual_target FROM pump_control WHERE id = 1")
                control = cursor.fetchone()
                
                if control and control.get('pause_end_time'):
                    pause_end_time = control['pause_end_time']
                    
                    if isinstance(pause_end_time, str):
                        pause_dt = datetime.fromisoformat(pause_end_time)
                    else:
                        pause_dt = pause_end_time
                    
                    if now >= pause_dt:
                        logger.info(f"🔄 Auto-check: Pause expired, updating status")
                        
                        new_status = calculate_pump_status(db, now)
                        
                        cursor.execute("""
                            INSERT INTO sensor_data (moisture_level, water_level, pump_status)
                            SELECT 
                                COALESCE((SELECT moisture_level FROM sensor_data ORDER BY created_at DESC LIMIT 1), 0),
                                COALESCE((SELECT water_level FROM sensor_data ORDER BY created_at DESC LIMIT 1), 0),
                                %s
                        """, (new_status,))
                        
                        logger.info(f"✅ Pause cleared, pump status = {new_status}")
                
                cursor.close()
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"❌ Auto-check error: {e}")

@app.on_event("startup")
async def startup():
    migrate_db()
    asyncio.create_task(auto_check_pause_expiry())
    logger.info("✅ System started - v9.0-FINAL-FIX")

@app.get("/")
async def root():
    return {
        "status": "online", 
        "version": "9.0-FINAL-FIX", 
        "fixes": ["correct_db_structure", "manual_target_ON_OFF_AUTO", "pause_logic", "1_day_chart"]
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
    """
    FIX: Get sensor history for the last 1 DAY (bukan 2 hari)
    """
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        cursor = db.cursor(dictionary=True)
        
        # FIX: 1 DAY instead of 2 DAYS
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
        logger.info(f"📊 Query returned {len(rows)} rows from last 1 day")
        
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
    """Save sensor data with proper validation"""
    db = get_db()
    if not db:
        logger.error("❌ Database connection failed")
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        now = get_local_time()
        
        # Validate and clamp data
        moisture = max(0, min(100, data.moisture_level))
        water = max(0, min(100, data.water_level))
        
        pump_status = calculate_pump_status(db, now)
        
        cursor = db.cursor()
        
        try:
            cursor.execute("""
                INSERT INTO sensor_data (moisture_level, water_level, pump_status, created_at)
                VALUES (%s, %s, %s, %s)
            """, (moisture, water, pump_status, now))
            
            insert_id = cursor.lastrowid
            logger.info(f"💾 ID={insert_id}: moisture={moisture}%, water={water}%, pump={pump_status}")
            
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
    """
    FIX: Gunakan manual_target dengan nilai ON/OFF/AUTO
    Saat PAUSE, simpan state sebelumnya agar bisa kembali setelah pause selesai
    """
    db = get_db()
    if not db:
        raise HTTPException(status_code=500, detail="DB offline")
    
    try:
        action = update.action.upper()
        now = get_local_time()
        
        logger.info(f"🎮 CONTROL ACTION: {action}")
        cursor = db.cursor(dictionary=True)
        
        if action == "PAUSE":
            minutes = update.minutes or 30
            pause_until = now + timedelta(minutes=minutes)
            
            # FIX: Saat pause, JANGAN ubah manual_target
            # Hanya set pause_end_time
            cursor.execute("""
                UPDATE pump_control 
                SET pause_end_time = %s 
                WHERE id = 1
            """, (pause_until,))
            
            logger.info(f"⏸️ Pause for {minutes} minutes until {pause_until}")
            msg = f"Pause set for {minutes} minutes"
        
        elif action == "MANUAL_ON":
            # FIX: Set manual_target = 'ON' dan clear pause
            cursor.execute("""
                UPDATE pump_control 
                SET manual_target = 'ON', pause_end_time = NULL 
                WHERE id = 1
            """)
            logger.info(f"🔌 manual_target = ON")
            msg = "Manual ON activated"
        
        elif action == "MANUAL_OFF":
            # FIX: Set manual_target = 'OFF' dan clear pause
            cursor.execute("""
                UPDATE pump_control 
                SET manual_target = 'OFF', pause_end_time = NULL 
                WHERE id = 1
            """)
            logger.info(f"🔌 manual_target = OFF")
            msg = "Manual OFF activated"
        
        elif action == "AUTO":
            # FIX: Set manual_target = 'AUTO' dan clear pause
            cursor.execute("""
                UPDATE pump_control 
                SET manual_target = 'AUTO', pause_end_time = NULL 
                WHERE id = 1
            """)
            logger.info(f"📅 manual_target = AUTO")
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
        
        # FIX: Set ke AUTO mode dan clear pause
        cursor.execute("""
            UPDATE pump_control 
            SET manual_target = 'AUTO', pause_end_time = NULL 
            WHERE id = 1
        """)
        
        cursor.close()
        logger.info(f"✅ Schedule saved, manual_target = AUTO")
        
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
        logger.info(f"🗑️ Deleting schedules")
        cursor = db.cursor()
        
        cursor.execute("DELETE FROM pump_schedules")
        deleted_count = cursor.rowcount
        
        cursor.execute("ALTER TABLE pump_schedules AUTO_INCREMENT = 1")
        
        # FIX: Set ke AUTO dan clear pause
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