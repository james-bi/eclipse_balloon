from picamera2 import Picamera2
import time

def take_mission_photo(filename="flight_image.jpg"):
    print(f"📸 Preparing camera...")
    
    try:
        # Initialize the camera
        picam2 = Picamera2()
        
        # Configure for a "Still" (high-resolution) photo
        config = picam2.create_still_configuration()
        picam2.configure(config)
        
        # Start the camera preview (internally)
        picam2.start()
        
        # ⚠️ CRUCIAL: Give the sensor 2 seconds to adjust to the light
        print("💡 Adjusting for light levels...")
        time.sleep(2)
        
        # Capture the shot
        print(f"🚀 SNAP! Saving to {filename}")
        picam2.capture_file(filename)
        
        # Clean up
        picam2.stop()
        print("✅ Photo saved successfully.")
        
    except Exception as e:
        print(f"❌ Camera Error: {e}")
        print("Check if the ribbon cable is loose!")

if __name__ == "__main__":
    take_mission_photo("scout_launch_test.jpg")
