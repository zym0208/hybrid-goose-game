from typing import Optional

import cv2 
import numpy as np 
from sklearn.cluster import DBSCAN 
import time
import threading
import queue

class CameraConfig:
    """
    Configuration for the dice detection camera.
    """
    stream_url: str = "http://172.20.10.3/stream"
    show_preview_window: bool = True
    blur_size: tuple = (5, 5)  #smooth the image (to remove noise)
    
    #Stability: how long the dice must be still
    stable_frames: int = 2 #Number of consecutive frames showing same number

    rearm_no_dice_s: float = 0.3  #wait 0.3 seconds with no dice before allowing new detection
    max_ready_age_s: float = 5  #detected value expires after 5 seconds
    
    #blob detection settings (dots on the dice)
    min_area: float = 60  #minimum size of a dot (in pixels squared)
    max_area: float = 300  #maximum size of a dot (in pixels squared)
    min_circularity: float = 0.5  #how round the dot must be (1=perfect circle)
    min_inertia_ratio: float = 0.3  #how evenly distributed the dot's mass is
    
    #clustering settings: how to group dots that belong to the same dice
    dbscan_eps: float = 180.0  #maximum distance between dots on same dice (in pixels)

class DiceCameraWorker(threading.Thread):
    """
    This is the main worker that runs continuously in the background, watching the camera feed and detecting dice.    
    how it works:
    1.grab frames from the camera continuously
    2.process each frame to find dark dots
    3.count the dots and group them by dice
    4.track stability
    5.report the detected number when it's stable and ready
    """
    
    def __init__(self, cfg: CameraConfig, q: queue.Queue):
        """
        Initialize the dice detector.
        
        Args:
            cfg: configuration settings
            q: queue to send results back to main program
        """
        super().__init__(daemon=True)  #run as background thread
        self.cfg = cfg
        self.q = q  #communication channel to send detected numbers
        self._running = True  #flag to control the main loop

        self._lock = threading.Lock()  #prevents race conditions in multi threading
        
        self._last_candidate: int = None  #last number we detected
        self._stable_count: int = 0  #how many frames in a row showed this number ( for stability tracking)
        
        self._ready_value: int = None  #the stable value
        self._ready_since: float = 0.0  #when we confirmed this value is stable
        
        self._armed: bool = True  #whether we are ready to detect a new dice
        self._no_dice_since: float = None  #when we last saw no dice
        
        self._preview_quit: bool = False  #flag for closing preview window

    def stop(self):
        """to tell the worker thread to stop running"""
        self._running = False

    def consume(self):
        """
        Called after the main program uses a detected value. This resets the detector so it waits for the dice to be removed
        before detecting again. This prevents double counting the same dice roll.
        """
        with self._lock:
            self._armed = False  #not ready to detect until dice is removed
            self._no_dice_since = None
            self._ready_value = None
            self._stable_count = 0
            self._last_candidate = None

    def get_ready_value(self):
        """
        Get the currently detected dice value if it is valid. Returns:
            The dice value or None
        """
        now = time.time()
        with self._lock:
            #cannot return value if the dice has not been removed since the last detection
            if not self._armed:
                return None
            if self._ready_value is None:
                return None
            
            #value expires after a certain time to prevent old detections
            if (now - self._ready_since) > self.cfg.max_ready_age_s:
                return None
            return int(self._ready_value)

    def _create_blob_detector(self):
        """
        Create the blob detector that finds dark dots on the dice.
        """
        params = cv2.SimpleBlobDetector_Params()

        #filter 1 :size
        #only detect blobs within a certain size range. Too small = noise, too large = not a dot
        params.filterByArea = True
        params.minArea = float(self.cfg.min_area)  #60 pixels squared
        params.maxArea = float(self.cfg.max_area)  #300 pixels squared
        
        #filter 2: circularity
        #dice dots are round so we filter by how circular the blob is
        #perfect circle = 1, straight line = 0
        params.filterByCircularity = True
        params.minCircularity = float(self.cfg.min_circularity)  # At least 50% circular
        
        #filter 3: inertia ratio
        #inertia ratio measures how elongated a shape is . High value = more round, low value = more stretched
        params.filterByInertia = True
        params.minInertiaRatio = float(self.cfg.min_inertia_ratio)  #0.3 minimum for our dots
        
        #filter 4: color
        #we are looking for dark blobs
        params.filterByColor = True
        params.blobColor = 0 #black 

        return cv2.SimpleBlobDetector_create(params)


    def _process_frame(self, frame, detector):
        """
        steps of the image processing pipeline:
        1.convert image to grayscale (simpler to analyze)
        2.blur the image (removes noise)
        3.detect all blobs
        4.group nearby blobs together (to separate multiple dice if we ever needed)
        5.count dots in the highest face group
        6.return the count
        
        Args:
            frame:the raw camera image (colored)
            detector:the blob detector we created earlier
            
        Returns:
            The dice value (1-6) or None
        """
 
        if frame is None:
            return None
        
        #greyscale
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        #blur the image slightly to remove noise and make dots clearer
        gray = cv2.GaussianBlur(gray, self.cfg.blur_size, 0)
        
        #detect all the dark circular blobs
        keypoints = detector.detect(gray)

        if not keypoints:
            return None
        
        #extract the (x, y) positions of all detected dots
        pts = np.array([kp.pt for kp in keypoints])
        
        #step 2: blur the image
        #gaussian blur smooths the image and removes small imperfections which helps
        #the blob detector focus on real dots and not noise
        #(5, 5) is the blur kernel size (bigger = more blur)
        try:
            clustering = DBSCAN(eps=self.cfg.dbscan_eps, min_samples=1).fit(pts)
            labels = clustering.labels_
        except Exception:
            return None
        
        #now we analyze each group of dots to figure out which is the top face
        cluster_info = []
        
        for cluster_id in np.unique(labels):
            #skip noise points
            if cluster_id == -1:
                continue
            
            #get all the dots that belong to this cluster
            cluster_mask = labels == cluster_id
            cluster_points = pts[cluster_mask]
            num_dots = int(np.sum(cluster_mask))
            
            #feature 1: calculate average Y position
            #the top face of the dice is higher in the image (smaller Y value)
            #Y=0 is the top of the image, Y increases going down so smaller Y = higher up
            avg_y = np.mean(cluster_points[:, 1])
            
            #feature 2:calculate the size of this cluster
            #the top face is usually larger because it is closer to the camera
            x_coords = cluster_points[:, 0]
            y_coords = cluster_points[:, 1]
            
            x_span = np.max(x_coords) - np.min(x_coords)
            y_span = np.max(y_coords) - np.min(y_coords)
            area = x_span * y_span
            
            #save all the information about this cluster
            cluster_info.append({
                'num_dots': num_dots,
                'avg_y': avg_y,
                'area': area
            })

        if not cluster_info:
            return None
        
        #get lists of all Y values and areas for normalization
        y_values = [c['avg_y'] for c in cluster_info]
        areas = [c['area'] for c in cluster_info]
        
        y_min = min(y_values)
        y_max = max(y_values)
        area_min = min(areas)
        area_max = max(areas)
        
        #calculate a score for each cluster to determine which is the top face
        for cluster in cluster_info:

            #normalize Y position to a 0-1 scale. we flip it so that higher in image (smaller Y) = higher score
            if y_max > y_min:
                norm_y = 1.0 - ((cluster['avg_y'] - y_min) / (y_max - y_min))
            else:
                norm_y = 1.0  #all clusters are at the same height
            
            #normalize area to a 0-1 scale
            #larger area = higher score
            if area_max > area_min:
                norm_area = (cluster['area'] - area_min) / (area_max - area_min)
            else:
                norm_area = 1.0  #all clusters are the same size

            #calculate final score as weighted average
            #60% based on position 40% based on size
            cluster['score'] = 0.6 * norm_y + 0.4 * norm_area

        #sort clusters by score (highest first)
        cluster_info.sort(key=lambda x: x['score'], reverse=True)
        
        #take the cluster with the highest score (top face)
        top_face = cluster_info[0]
        v = top_face['num_dots']

        if 1 <= v <= 6:
            return int(v)

        return None

    def run(self):
        """
        1.grab frame from camera
        2.process frame to detect dice value
        3.update stability tracking
        4.manage armed state
        5.send updates to main program
        6.repeat until stopped
        """
        if cv2 is None or np is None or DBSCAN is None:
            self.q.put(("log", "Camera disabled: missing dependencies"))
            self.q.put(("cam", {"armed": False, "ready": False, "value": None}))
            return

        url = self.cfg.stream_url
        self.q.put(("log", f"Camera connecting: {url}"))

        cap = cv2.VideoCapture(url)
        
        #set buffer size to 1 so we always get the most recent frame
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        if not cap.isOpened():
            self.q.put(("log", "Camera: could not open stream"))
            self.q.put(("cam", {"armed": False, "ready": False, "value": None}))
            return

        detector = self._create_blob_detector()

        if self.cfg.show_preview_window:
            cv2.namedWindow("ESP32 Dice", cv2.WINDOW_NORMAL)
        
        last_emit = 0.0  #track when we last sent an update
        
        #main loop
        while self._running and not self._preview_quit:
            
            ret, frame = cap.read()
            if not ret:
                self.q.put(("log", "Camera frame lost, reconnecting..."))
                time.sleep(0.5)
                cap.open(url)
                continue

            candidate = self._process_frame(frame, detector)
            now = time.time()
            
            #state machine logic
            #this controls when we are ready to detect and when we are waiting
            with self._lock:
                
                #case 1: waiting for dice to be removed
                if not self._armed:
                    if candidate is None:
                        #start timing how long there is been no dice
                        if self._no_dice_since is None:
                            self._no_dice_since = now
                        
                        #if no dice for long enough re-arm the detector
                        elif (now - self._no_dice_since) >= self.cfg.rearm_no_dice_s:
                            self._armed = True
                            self._no_dice_since = None
                            self._stable_count = 0
                            self._last_candidate = None
                            self._ready_value = None
                    else:
                        #dice still in view reset the timer
                        self._no_dice_since = None
                
                #case 2: armed and looking for stable dice value
                if self._armed:
                    if candidate is None:
                        #no dice detected reset everything
                        self._last_candidate = None
                        self._stable_count = 0
                        self._ready_value = None
                    else:
                        #dice detected

                    #we need to see the same number multiple times in a row to confirm it is stable
                        
                        if candidate == self._last_candidate:
                            #same as last frame, increment stability counter
                            self._stable_count += 1
                        else:
                            #different from last frame, reset counter
                            self._last_candidate = candidate
                            self._stable_count = 1

                        if self._stable_count >= self.cfg.stable_frames:
                            self._ready_value = candidate
                            self._ready_since = now

                armed = self._armed
                ready_val = self._ready_value if armed else None
                ready = ready_val is not None

            if (now - last_emit) >= 0.15:
                self.q.put(("cam", {
                    "armed": armed,      #ready to detect
                    "ready": ready,      #stable value available
                    "value": ready_val   #the detected value
                }))
                last_emit = now

            if self.cfg.show_preview_window:
                cv2.imshow("ESP32 Dice", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    self._preview_quit = True

        cap.release()
        if self.cfg.show_preview_window:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass
