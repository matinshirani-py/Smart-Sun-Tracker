import cv2
import numpy as np
import time
from picamera2 import Picamera2

# ============================================================
# CONFIGURATION PARAMETERS
# ============================================================


MIN_SUN_AREA_RATIO = 0.0007          
MAX_SUN_AREA_RATIO = 0.1             

ABSOLUTE_MIN_BRIGHTNESS = 100        

TRACKING_WINDOW = 80                 
TRACK_LOST_FRAMES = 10               
START_CONFIRM_FRAMES = 3             

FULL_SCAN_INTERVAL = 3.0             

# Animation parameters
ANIMATION_INTERVAL = 6.0             
ANIMATION_DURATION = 0.8             

YELLOW = (0, 255, 255)
RED    = (0, 0, 255)
WHITE  = (255, 255, 255)
GREEN  = (0, 255, 0)

def crop_roi(frame, center, window_size):
    h, w = frame.shape[:2]
    cx, cy = center
    
    half = window_size // 2
    x1, y1 = max(0, cx - half), max(0, cy - half)
    x2, y2 = min(w, cx + half), min(h, cy + half)
    
    return frame[y1:y2, x1:x2], x1, y1

def detect_sun_dynamic(frame, offset_x=0, offset_y=0):
    if frame is None or frame.size == 0:
        return None
    
    h, w = frame.shape[:2]
    total_pixels = h * w
    sky_top_limit = int(0.90 * h)
    
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    value_channel = hsv[:, :, 2]
    
    sky_region = np.zeros_like(value_channel)
    sky_region[:sky_top_limit, :] = 255
    value_channel = cv2.bitwise_and(value_channel, sky_region)
    
    _, max_val, _, _ = cv2.minMaxLoc(value_channel)
    
    if max_val < ABSOLUTE_MIN_BRIGHTNESS:
        return None
        
    dynamic_thresh = max(ABSOLUTE_MIN_BRIGHTNESS, max_val - 15)
    _, bright_mask = cv2.threshold(value_channel, dynamic_thresh, 255, cv2.THRESH_BINARY)
    
    kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    bright_mask = cv2.morphologyEx(bright_mask, cv2.MORPH_OPEN, kernel_open)
    bright_mask = cv2.morphologyEx(bright_mask, cv2.MORPH_CLOSE, kernel_close)
    
    contours, _ = cv2.findContours(bright_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    best_candidate = None
    max_area = -1
    
    for cnt in contours:
        area_px = cv2.contourArea(cnt)
        if area_px < total_pixels * MIN_SUN_AREA_RATIO or area_px > total_pixels * MAX_SUN_AREA_RATIO:
            continue
            
        M = cv2.moments(cnt)
        if M["m00"] == 0:
            continue
            
        cx = int(M["m10"] / M["m00"]) + offset_x
        cy = int(M["m01"] / M["m00"]) + offset_y
        
        if area_px > max_area:
            max_area = area_px
            radius_est = int(np.sqrt(area_px / np.pi))
            best_candidate = {
                'center': (cx, cy),
                'radius_est': radius_est,
                'thresh_used': dynamic_thresh
            }
            
    return best_candidate

class SunTracker:
    def __init__(self):
        print("Initializing Picamera2...")
        self.picam2 = Picamera2()
        
        self.frame_width = 320
        self.frame_height = 240
        self.fps = 15
        
        config = self.picam2.create_preview_configuration(main={"size": (self.frame_width, self.frame_height)})
        self.picam2.configure(config)
        self.picam2.start()
        
        self.last_full_search_time = 0
        self.detect_counter = 0
        self.last_center = None
        self.tracking = False
        self.track_lost = 0
        self.search_window = TRACKING_WINDOW
        
        # متغیرهای کنترل انیمیشن
        self.last_anim_trigger = time.time()
        self.animating = False
        self.anim_start_time = 0
        
    def search_sun(self, frame, current_time):
        force_full_scan = (current_time - self.last_full_search_time) > FULL_SCAN_INTERVAL
        
        if self.tracking and self.last_center is not None and not force_full_scan:
            roi, ox, oy = crop_roi(frame, self.last_center, self.search_window)
            if roi.shape[0] > 10 and roi.shape[1] > 10:
                result = detect_sun_dynamic(roi, ox, oy)
                if result is not None:
                    self.track_lost = 0
                    self.last_center = result["center"]
                    return result, "ROI"
            
            self.track_lost += 1
            if self.track_lost >= TRACK_LOST_FRAMES:
                self.tracking = False
                self.last_center = None
                
        result = detect_sun_dynamic(frame)
        self.last_full_search_time = current_time 
        
        if result is not None:
            self.tracking = True
            self.track_lost = 0
            self.last_center = result["center"]
            
        return result, "FULL"
        
    def run(self):
        try:
            while True:
                frame = self.picam2.capture_array()
                frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                
                time.sleep(0.03)
                current_time = time.time()
                
                sun_data = self.search_sun(frame, current_time)
                sun, search_mode = sun_data if sun_data else (None, "NONE")
                
                if sun is not None:
                    self.detect_counter += 1
                else:
                    self.detect_counter = 0
                    
                detected = self.detect_counter >= START_CONFIRM_FRAMES
                
                if detected:
                    cx, cy = sun["center"]
                    radius = sun["radius_est"]
                    thresh = sun["thresh_used"]
                    
                    cv2.circle(frame, (cx, cy), radius, YELLOW, 2)
                    cv2.circle(frame, (cx, cy), 3, RED, -1)
                    
                    mode_text = f"TRACKING ({search_mode})"
                    cv2.putText(frame, mode_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, GREEN, 2)
                    cv2.putText(frame, f"Center: ({cx}, {cy})", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.4, YELLOW, 1)
                    cv2.putText(frame, f"Dyn-Thresh: {thresh}", (10, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.4, WHITE, 1)
                    
                    if search_mode == "ROI" and self.last_center is not None:
                        half = self.search_window // 2
                        cx2, cy2 = self.last_center
                        cv2.rectangle(frame, (cx2 - half, cy2 - half), (cx2 + half, cy2 + half), WHITE, 1)

                    if current_time - self.last_anim_trigger >= ANIMATION_INTERVAL:
                        self.animating = True
                        self.anim_start_time = current_time
                        self.last_anim_trigger = current_time

                    if self.animating:
                        elapsed = current_time - self.anim_start_time
                        if elapsed <= ANIMATION_DURATION:
                            progress = elapsed / ANIMATION_DURATION
                            
                            start_x1, start_y1 = 0, 0
                            start_x2, start_y2 = self.frame_width, self.frame_height
                
                            end_x1, end_y1 = cx - radius, cy - radius
                            end_x2, end_y2 = cx + radius, cy + radius
                            
          
                            draw_x1 = int(start_x1 + (end_x1 - start_x1) * progress)
                            draw_y1 = int(start_y1 + (end_y1 - start_y1) * progress)
                            draw_x2 = int(start_x2 + (end_x2 - start_x2) * progress)
                            draw_y2 = int(start_y2 + (end_y2 - start_y2) * progress)
                            
                            cv2.rectangle(frame, (draw_x1, draw_y1), (draw_x2, draw_y2), YELLOW, 1)
                        else:
                            self.animating = False
                    # -------------------------------------------

                else:
                    cv2.putText(frame, "SEARCHING (FULL)", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, YELLOW, 2)
                    self.animating = False
                cv2.imshow("Smart Sun Tracker V7", frame)
                
                key = cv2.waitKey(1) & 0xFF
                if key == 27 or key == ord('q'):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.picam2.stop()
            cv2.destroyAllWindows()

if __name__ == "__main__":
    tracker = SunTracker()
    tracker.run()