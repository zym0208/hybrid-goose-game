import cv2
import numpy as np
from sklearn.cluster import DBSCAN
import time

# ================= Configuration =================
ESP32_IP = "192.168.137.39"  # <--- TODO: Update with your ESP32 IP
STREAM_URL = f"http://{ESP32_IP}/stream"

# Image Processing Config
BLUR_SIZE = (5, 5)          # Gaussian blur kernel size for noise reduction
# ===============================================

def create_blob_detector():
    """Setup blob detector parameters (Tune these for lighting/distance)"""
    params = cv2.SimpleBlobDetector_Params()
    
    # Filter by Area (Size of the pips)
    params.filterByArea = True
    params.minArea = 80       # Min pip size (increase if detecting noise)
    params.maxArea = 400      # Max pip size
    
    # Filter by Shape
    params.filterByCircularity = True
    params.minCircularity = 0.5
    params.filterByInertia = True
    params.minInertiaRatio = 0.3
    
    # Filter by Color
    params.filterByColor = True
    params.blobColor = 0      # 0 = Black pips, 255 = White pips
    
    return cv2.SimpleBlobDetector_create(params)

def put_label(img, text, pos, color=(0, 0, 255)):
    x, y = pos
    cv2.putText(img, str(text), (x, y), cv2.FONT_HERSHEY_SIMPLEX, 
                1.2, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(img, str(text), (x, y), cv2.FONT_HERSHEY_SIMPLEX, 
                1.2, color, 2, cv2.LINE_AA)

def process_frame(frame, detector):
    if frame is None: return frame, []
    
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # Apply blur to handle low-quality camera noise
    gray_blurred = cv2.GaussianBlur(gray, BLUR_SIZE, 0)
    
    keypoints = detector.detect(gray_blurred)
    if not keypoints: return frame, []

    pts = np.array([kp.pt for kp in keypoints])

    # DBSCAN Clustering: Groups pips into dice
    # eps: Max distance between pips. Increase if pips on same die are split.
    try:
        clustering = DBSCAN(eps=80, min_samples=1).fit(pts)
        labels = clustering.labels_
    except:
        return frame, []

    dice_info = []
    for die_id in np.unique(labels):
        if die_id == -1: continue # Noise
        
        mask = labels == die_id
        count = int(np.sum(mask))
        
        cluster_pts = pts[mask].astype(int)
        x_min, y_min = cluster_pts.min(axis=0)
        x_max, y_max = cluster_pts.max(axis=0)
        cx = int(cluster_pts[:, 0].mean())
        cy = int(cluster_pts[:, 1].mean())

        cv2.rectangle(frame, (x_min - 10, y_min - 10), (x_max + 10, y_max + 10), (0, 255, 0), 2)
        put_label(frame, count, (cx, cy), color=(0, 255, 255))
        dice_info.append(count)

    # Debug: Draw red circles around detected keypoints
    frame = cv2.drawKeypoints(frame, keypoints, None, (0, 0, 255), 
                              cv2.DRAW_MATCHES_FLAGS_DRAW_RICH_KEYPOINTS)
    return frame, dice_info

def main():
    print(f"Connecting to: {STREAM_URL}")
    cap = cv2.VideoCapture(STREAM_URL)
    
    # Minimize buffer to reduce latency
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not cap.isOpened():
        print("Error: Could not connect to stream. Check IP.")
        return

    detector = create_blob_detector()
    cv2.namedWindow("ESP32 Dice", cv2.WINDOW_NORMAL)

    while True:
        ret, frame = cap.read()

        if not ret:
            print("Frame lost, retrying connection...")
            time.sleep(1)
            cap.open(STREAM_URL) # Reconnect attempt
            continue

        out_frame, counts = process_frame(frame, detector)

        if counts:
            total = sum(counts)
            put_label(out_frame, f"Total: {total}", (20, 40), color=(50, 255, 50))
        else:
            put_label(out_frame, "No Dice", (20, 40), color=(50, 50, 200))

        cv2.imshow("ESP32 Dice", out_frame)

        if cv2.waitKey(10) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()