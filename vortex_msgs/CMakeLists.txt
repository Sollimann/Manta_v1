cmake_minimum_required(VERSION 2.8.3)
project(vortex_msgs)

find_package(catkin REQUIRED COMPONENTS
  message_generation
  geometry_msgs
  std_msgs
  actionlib
  actionlib_msgs
)

add_message_files(
  DIRECTORY msg
  FILES
  ThrusterForces.msg
  LightInput.msg
  PropulsionCommand.msg
  EulerOrientations.msg
  Debug.msg
  Pwm.msg
  RovState.msg
  ContainerID.msg
  Manipulator.msg
  CameraFeedSelection.msg
  CameraObjectInfo.msg
)

## Generate services in the 'srv' folder
add_service_files(
   FILES
    ControlMode.srv
    SetBatteryLevel.srv
#   Service2.srv
)

add_action_files(
  FILES
  MoveRelative.action
  LosPathFollowing.action
)

generate_messages(
  DEPENDENCIES
  geometry_msgs
  std_msgs
  actionlib_msgs
)

catkin_package(
  CATKIN_DEPENDS
  message_runtime
  geometry_msgs
  std_msgs
)
