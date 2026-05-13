import subprocess
import time
import sys

def get_telemetry():
    """Fetches and parses GPS data from the Quectel modem."""
    print("🛰️  Checking satellite link...")
    
    try:
        # Requesting location data (Format 2)
        # Output format: <UTC>,<lat>,<lon>,<hdop>,<alt>,<fix>,<cog>,<spkm>,<spkn>,<date>,<nsat>
        cmd = "echo 'AT+QGPSLOC=2' | sudo socat - /dev/ttyUSB2,crnl"
        raw_output = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, timeout=5).decode().strip()

        if "+QGPSLOC:" in raw_output:
            # Clean the string to get just the numbers
            data_string = raw_output.split("+QGPSLOC: ")[1]
            parts = data_string.split(",")
            
            telemetry = {
                "time": parts[0],
                "lat":  parts[1],
                "lon":  parts[2],
                "alt":  parts[4],
                "sats": parts[10]
            }
            
            print(" ✅ FIX ACQUIRED!")
            print(f" 📍 Position: {telemetry['lat']}, {telemetry['lon']}")
            print(f" 🏔️ Altitude: {telemetry['alt']} meters MSL")
            print(f" 📡 Satellites in view: {telemetry['sats']}")
            return telemetry

        elif "516" in raw_output:
            print(" ❌ STATUS: NO FIX.")
            print("    Reason: The antenna can't see enough satellites yet.")
            print("    Action: Move the sticker antenna outside or away from buildings.")
            
        elif "505" in raw_output:
            print(" ❌ STATUS: GPS ENGINE OFF.")
            print("    Action: Run the 'initialize_flight_gps' script first!")
            
        else:
            print(f" ❓ UNKNOWN ERROR: {raw_output}")

    except Exception as e:
        print(f" 🛠️ HARDWARE ERROR: Could not talk to the modem. ({str(e)})")
        print("    Check: Is the Sixfab HAT securely attached?")
    
    return None

if __name__ == "__main__":
    # Simple loop for the Scouts to watch the signal build up
    print("--- 🎈 BALLOON TRACKER STARTING ---")
    try:
        while True:
            get_telemetry()
            print("-" * 30)
            print("Next check in 10 seconds... (Press Ctrl+C to stop)")
            time.sleep(10)
    except KeyboardInterrupt:
        print("\nStopping tracker. Good luck with the launch!")
