"""webrtc_detect —— detect 模式在 ROS 2 里的节点实现。

三个东西，各管一段：

    camera_node    采集（GStreamer v4l2src → BGR appsink）→ sensor_msgs/Image
    detector_node  订阅原图 → OpenCV 预处理 → RKNN → 画框 → 标注图 + JSON
    detector_core  上面两个节点共用的纯函数（预处理/解码/NMS/画框/白平衡）

WebRTC 那一端不在这里：它是 `src/device_agent.py --mode ros`，只订阅标注图。
"""
