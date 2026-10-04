#!/bin/bash
# 启动机器人侧 ROS2<->TCP bridge（在 .163 上运行）
cd "$(dirname "$0")"
source /opt/ros/humble/setup.bash
source /apps/engineai_robotics/setup_board_ros_env.bash
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_LOCALHOST_ONLY=0
exec python3 bridge.py --port 9801 --rate 200 --slew 3.0
