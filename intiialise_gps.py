import serial
import time
import logging
import subprocess

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("GPS_Init")

# --- CONFIGURATION ---
SERIAL_PORT = "/dev/ttyUSB2"  # Command port for Quectel modules
BAUD_RATE = 115200
TIMEOUT = 2

def run_at_command(ser, command, wait_time=1):
    """Sends an AT command and logs the response."""
    full_command = f"{command}\r\n"
    logger.info(f"Sending: {command}")
    ser.write(full_command.encode())
    
    time.sleep(wait_time)
    
    response = ser.read_all().decode(errors='ignore').strip()
    if "OK" in response:
        logger.info(f"✅ Success: {command}")
        return True
    else:
        logger.warning(f"❌ Issue with {command}: {response}")
        return False

def initialize_gps():
    # 1. Kill ModemManager so it doesn't hijack the port
    logger.info("Stopping ModemManager to free up the serial port...")
    subprocess.run(["sudo", "systemctl", "stop", "ModemManager"], check=False)
    
    try:
        with serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=TIMEOUT) as ser:
            logger.info(f"--- Starting GPS Mission Prep on {SERIAL_PORT} ---")

            # A. Turn GPS OFF first (essential to change settings)
            run_at_command(ser, "AT+QGPS=0")
            
            # B. Set Dynamic Model to 6 (Airborne < 1g)
            # This ensures the GPS doesn't 'freeze' at 12,000 meters!
            run_at_command(ser, 'AT+QGPSCFG="dynamicmodel",6')

            # C. Turn GPS back ON
            if run_at_command(ser, "AT+QGPS=1"):
                logger.info("🚀 GPS Engine is now ACTIVE!")
            
            # D. Inject XTRA Data (Assisted GPS) for a faster fix
            logger.info("Attempting XTRA Assisted-GPS injection...")
            run_at_command(ser, "AT+QGPSXTRA=1")
            run_at_command(ser, "AT+QGPSXTRADATA=1")

            logger.info("--- Initialization Complete! ---")
            logger.info("Bhai, take the Pi outside now. Waiting for the satellites to say hello.")

    except Exception as e:
        logger.error(f"FATAL ERROR: {e}")

if __name__ == "__main__":
    initialize_gps()
