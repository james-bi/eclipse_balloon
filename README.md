# 🚀 Eclipse Balloon: The Flight Computer 

Welcome to the brains of the Eclipse Balloon project! This is the actual code that will fly to the edge of space on a high-altitude balloon. Our tiny but mighty **Raspberry Pi Zero** flight computer is built to endure the extreme cold of the stratosphere and the rough journey back down to Earth.

Whether you're a Scout working on your STEM badges or an aspiring rocket scientist, this guide will show you exactly how our spacecraft thinks and operates.

### Prerequisites (Raspberry Pi Setup)
Before running the flight computer on the Raspberry Pi, ensure you have the necessary system packages installed to interact with the hardware and modem:
```bash
sudo apt-get update
sudo apt-get install -y gpsd gpsd-clients modemmanager iproute2 usbutils
```
### Prerequisites (Raspberry Pi Flight Preparation)

1: Get to working directory in the pi, run these commands:

```bash
cd Repos/balloon/
source venv/bin/activate
cd eclipse_balloon/
```

The prompt mshould now look like this, note `venv`

```bash
(venv) scouts@scoutspi:~/Repos/balloon/eclipse_balloon $
```

2: Check networks connected:

```bash
nmcli device status
```

Output needs to include `wlan0` equal to `wifi / connected` and `usb0` equal to `ethernet Sixfab4G` *IMPORTANT* as so:

```bash
DEVICE         TYPE      STATE                   CONNECTION  
wlan0          wifi      connected               MIWIFI_gRGQ 
usb0           ethernet  connected               Sixfab_4G   
lo             loopback  connected (externally)  lo          
p2p-dev-wlan0  wifi-p2p  disconnected            --     
```

3: Initialise GPS - This could take up to 20 minutes:

Make sure the prompt looks like this, if not go back and do step 1 again:

```bash
(venv) scouts@scoutspi:~/Repos/balloon/eclipse_balloon $
```

Run the GPS intialise command:

```bash
python initialise_gps.py
```

Wait for these lines:

```bash
[INFO] --- Initialization Complete! ---
[INFO] Bhai, take the Pi outside now. Waiting for the satellites to say hello.
```

Run the GPS test command, this finds satellites, so it may take some time, up to 20 minutes:

```bash
python test_gps.py
```

### 🚀 Quick Start
When starting the flight computer, you can give your balloon a custom name (which overrides the `BALLOON_ID` in your `.env` file) by passing the `--name` argument:
```bash
python3 flight_loop.py --name "SCOUT_ECLIPSE_1"
```
This project supports both mock simulations and real field tests. For ground testing with real camera, GPS, and cellular connectivity, use:
```bash
sudo ./venv/bin/python3 flight_loop.py --name "Walk_Cellular_Test" --ground-test
```

The flight computer uses direct **Quectel modem access**, not gpsd, for bulletproof GPS reliability on real flights and field tests. This keeps the code simple and avoids daemon complexity.

**Altitude-based stage control** for tethered or incremental tests:
- `--ascent-low-alt <meters>`: upper altitude bound for `ASCENT_LOW` stage
- `--near-space-alt <meters>`: altitude at which the system enters `NEAR_SPACE`
- `--emergency-release-alt <meters>`: keep the system in `ASCENT_LOW` until this altitude is exceeded, then resume normal flight profile

Example for a tethered altitude test:
```bash
sudo ./venv/bin/python3 flight_loop.py --name "Tether_Test" --ascent-low-alt 1000 --near-space-alt 12000 --emergency-release-alt 1000
```

> **Note:** `--ground-test` cannot be used together with `--mock`. For actual flight or tethered tests (without `--ground-test`), GPS defaults to the direct Quectel modem method.
To test the cellular connection while maintaining local network access (e.g., for SSH), use the `--no-wifi` flag. This will remove the default internet route on the WiFi interface, forcing outbound internet traffic to use the cellular connection:
```bash
python3 flight_loop.py --no-wifi
```

---

## 🧠 How the Computer Works

Imagine trying to text your friends while strapped to a rollercoaster that goes 100,000 feet in the air! That's what our computer does. Every 5 seconds, it goes through a rapid checklist:

1. **Check the Sensors:** It reads the GPS to find out exactly where it is, how high it is, and how cold it's getting.
2. **Where are we?** It uses the altitude to figure out what "phase" of the journey it's in (like launching, space, or falling).
3. **Keep the Signal Alive:** Just like a cell phone drops calls in the wilderness, our balloon loses signal in the stratosphere. A special background helper called the `NetworkHealer` acts like an invisible astronaut. It constantly checks the internet connection. If the connection drops, it will automatically restart the modem or even pull the power plug (a "hard reset") to try and fix it!
4. **Save the Battery:** When the balloon reaches 15,000 meters (way higher than commercial airplanes!), it gets so cold that batteries struggle to work. To save energy, our computer actually turns off its internet connection and goes into "stealth mode" until it falls back down to a warmer altitude!

---

## 🌎 The Journey: Flight Phases

Our balloon's journey is broken down into five epic chapters:

*   🌱 **GROUND:** The balloon is on the launchpad. The computer wakes up, connects to the internet, and gets ready for liftoff.
*   🚀 **ASCENT (Going Up!):** From liftoff to 24,000 meters. The balloon is rising fast. The computer is busy texting Mission Control all of its data (telemetry).
*   🌌 **NEAR SPACE:** Above 24,000 meters. The sky turns black, and you can see the curve of the Earth. Cell phone towers don't reach up here! The computer stops trying to text and instead writes all its data into a local "black box" diary (`flight_log.json`).
*   🪂 **DESCENT (The Fall):** The balloon pops (on purpose!) and the parachute opens. Once it falls below 24,000 meters, the computer turns the internet back on and frantically tries to send all the diary entries it saved while it was in space.
*   🎯 **LANDED:** Touchdown! When the computer realizes it is close to the ground, it sends out one final, super-important GPS signal so we can find it.

---

## 🔥 Safety First: Fire Risk Actions

Safety is the most important part of any space mission! A computer running inside an insulated, protective box can get really hot when it lands on the ground. To make sure there is zero risk of a fire, our computer has built-in safety rules:

*   **The Final Shutdown:** When the computer calculates that it is about to hit the ground (or is less than 100 meters high), it goes into a complete lockdown. It sends its final location, closes all its files, and issues a special `sudo halt -p` command. This completely turns off the power to the Raspberry Pi, making it safe to handle when the recovery team finds it.
*   **Emergency Low Battery:** If the battery drops below 5% at any time, the computer will automatically shut itself down safely to prevent damage.
*   **Cooling in Space:** The internet modem creates a lot of heat. By turning it off above 15,000 meters, we save battery and keep the computer from getting dangerously hot when there is no air around to cool it down.

---

## 📡 Talking to Mission Control (The API)

How do we actually see the data? The flight computer sends its information over the cellular network to our Mission Control servers using something called an **API** (Application Programming Interface). Think of it like sending a very specific digital postcard.

Here is what the postcard looks like (in a language called JSON):
```json
{
  "balloon_id": "SCOUT_ECLIPSE_1",
  "latitude": 40.4168,
  "longitude": -3.7038,
  "altitude": 12500.5,
  "temperature": -20.5,
  "battery_level": 85.2
}
```

If the balloon lands, it adds a special red alert message to the postcard: `"event_type": "LANDING"`.

We also have an emergency alert system! If the `NetworkHealer` fixes a broken internet connection, it automatically sends a message to our Mission Control chatroom (like Discord or Slack) with its secret IP addresses so our engineers can log in remotely and check on the systems!

---
*Ad Astra! (To the Stars!)* ✨
