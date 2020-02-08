#!/usr/bin/env python
# Written by Kristoffer Rakstad Solberg, Student
# Copyright (c) 2020 Manta AUV, Vortex NTNU.
# All rights reserved.

import	rospy
import  math
import numpy as np
from    time import sleep
from	collections import OrderedDict
from	smach	import	State, StateMachine		
from    nav_msgs.msg import Odometry    
from	smach_ros	 import	SimpleActionState, IntrospectionServer	
from    move_base_msgs.msg  import  MoveBaseAction, MoveBaseGoal
from    vortex_msgs.msg import LosPathFollowingAction, LosPathFollowingGoal
from 	vortex_msgs.msg import PropulsionCommand
from 	geometry_msgs.msg import Wrench, Pose
from 	tf.transformations import euler_from_quaternion

# import mission plan
from finite_state_machine.mission_plan import *

# import object detection
from	vortex_msgs.msg import CameraObjectInfo

# camera centering controller
from autopilot.autopilot import CameraPID

# Imported help functions from src/finite_state_machine/
from    finite_state_machine import ControllerMode, WaypointClient, PathFollowingClient

#ENUM
OPEN_LOOP           = 0
POSE_HOLD           = 1
HEADING_HOLD        = 2
DEPTH_HEADING_HOLD  = 3 
DEPTH_HOLD          = 4
POSE_HEADING_HOLD   = 5
CONTROL_MODE_END    = 6

class ControlMode(State):

    def __init__(self, mode):
        State.__init__(self, ['succeeded','aborted','preempted'])
        self.mode = mode
        self.control_mode = ControllerMode()

    def execute(self, userdata):

        # change control mode
        self.control_mode.change_control_mode_client(self.mode)
        rospy.loginfo('changed DP control mode to: ' + str(self.mode) + '!')
        return 'succeeded'

# A list of tasks to be done
task_list = {'docking':['transit'],
			 'gate':['searching','detect','camera_centering','path_planning','tracking', 'passed'],
			 'pole':['searching','detect','camera_centering','path_planning','tracking', 'passed']
			}

def update_task_list(target, task):
    task_list[target].remove(task)
    if len(task_list[target]) == 0:
        del task_list[target]

class SearchForTarget(State):

	def __init__(self, target):
		State.__init__(self,outcomes=['found','unseen','passed','missed'],
							output_keys=['px_output','fx_output', 'search_output', 'search_confidence_output'])

		self.target = target
		self.search_timeout = 30.0
		self.sampling_time = 0.2
		self.timer = 0.0
		self.task_status = 'missed'

		# search for object
		if target == 'gate':
			self.sub_object = rospy.Subscriber('/gate_midpoint', CameraObjectInfo, self.objectDetectionCallback, queue_size=1)
		else:
			self.sub_object = rospy.Subscriber('/pole_midpoint', CameraObjectInfo, self.objectDetectionCallback, queue_size=1)

		self.pub_thrust = rospy.Publisher('/manta/thruster_manager/input', Wrench, queue_size=1)
		
		
		# thrust message
		self.thrust_msg = Wrench()

	def objectDetectionCallback(self, msg):

		""" detection frame
		(0,0)	increase->
		----------------> X
		|
		|
		| increase 
		|	 |
		|    v
		v
		
		Y

		"""

		self.object_px = msg.pos_x
		self.object_py = msg.pos_y
		self.object_fx = msg.frame_width
		self.object_fy = msg.frame_height
		self.object_confidence = msg.confidence
		self.object_distance = msg.distance_to_pole


	def execute(self, userdata):

		rospy.loginfo('Searching for ' + self.target)

		sleep(self.sampling_time)
		self.timer += self.sampling_time

		if self.timer > self.search_timeout:
			return self.task_status

		if self.object_px >= 0.0 and self.object_py >= 0.0:
			rospy.loginfo(self.target + ' found')

			# output the object pixel position
			userdata.px_output = self.object_px
			userdata.fx_output = self.object_fx
			userdata.search_confidence_output = self.object_confidence
			userdata.search_output = 'found'
			self.task_status = 'passed'

			return 'found'
		else:
			rospy.loginfo(self.target + ' not found')
			userdata.px_output = self.object_px
			userdata.fx_output = self.object_fx
			userdata.search_confidence_output = self.object_confidence
			userdata.search_output = 'unseen'
			return 'unseen'

class TrackTarget(State):

	def __init__(self, search_target, search_area):
		State.__init__(self, outcomes=['succeeded','aborted','preempted'],
							input_keys=['px_input','fx_input','search_input','search_confidence_input'])
		

		# initialize controller
		self.CameraPID = CameraPID()
		self.pub_thrust = rospy.Publisher('/manta/thruster_manager/input', Wrench, queue_size=1)
		
		# thrust message
		self.thrust_msg = Wrench()

		# my current pose
		self.vehicle_odom = Odometry()
		self.sub_pose = rospy.Subscriber('/odometry/filtered', Odometry, self.positionCallback, queue_size=1)

		# set init_flag
		self.init_flag = True
		
		# straight-line path segment
		self.search_target = search_target
		self.search_y = search_area.y
		self.search_x = search_area.x



	def positionCallback(self, msg):

		self.vehicle_odom = msg
		self.time = msg.header.stamp.to_sec()

		global roll, pitch, yaw
		orientation_q = msg.pose.pose.orientation
		orientation_list = [orientation_q.x, orientation_q.y, orientation_q.z, orientation_q.w]
		(roll,pitch,yaw) = euler_from_quaternion(orientation_list)

		self.psi = yaw

	def pathPlanning(self):

		# straight-line path segment
		y_delta = self.search_y - self.vehicle_odom.pose.pose.position.y
		x_delta = self.search_x - self.vehicle_odom.pose.pose.position.x

		# angle
		self.search_direction = np.arctan2(y_delta, x_delta) 


	def pathTracking(self):

		pass

	def nav_result_cb(self, userdata, status, result):

		if status == GoalStatus.PREEMPTED:
			rospy.loginfo("Waypoint preempted")
		if status == GoalStatus.SUCCEEDED:
			rospy.loginfo("Waypoint succeeded")

	def alignWithTarget(self, object_fx, object_px, search_input, object_confidence):

		tau_heave = self.CameraPID.depthController(-0.5, self.vehicle_odom.pose.pose.position.z, self.time)
		self.thrust_msg.force.z = tau_heave
		target_center_screen = object_fx*(0.60)

		if search_input == 'found' and object_confidence >= 1.0:
			# fix bounding boxes, their center values are calculated wrong and are not in center

			tau_sway = self.CameraPID.swayController(target_center_screen, object_px, self.time)
			tau_surge = self.CameraPID.speedController(0.1, self.vehicle_odom.twist.twist.linear.x, self.time)
			
			# you're on the right path, keep this heading
			tau_heading = self.CameraPID.headingController(self.psi, self.psi, self.time)

			#publish

			self.thrust_msg.torque.z = tau_heading
			self.thrust_msg.force.x = tau_surge
			self.thrust_msg.force.y = tau_sway

		else:

			tau_surge = self.CameraPID.speedController(0.3, self.vehicle_odom.twist.twist.linear.x, self.time)
			tau_heading = self.CameraPID.headingController(self.search_direction, self.psi, self.time)
			tau_sway = 0.0

			#publish
			self.thrust_msg.torque.z = tau_heading
			self.thrust_msg.force.x = tau_surge
			self.thrust_msg.force.y = tau_sway

		self.pub_thrust.publish(self.thrust_msg)


	def execute(self, userdata):

		sleep(0.2)
		# track the target
		nav_goal = LosPathFollowingGoal()	
		
		# initialize the direction you want to be looking
		if self.init_flag is True:
			self.pathPlanning()
			self.init_flag = False

		
		self.alignWithTarget(userdata.fx_input,userdata.px_input, userdata.search_input, userdata.search_confidence_input)
		self.pathTracking()

		return 'succeeded'


class TaskManager():

	def __init__(self):

		# init node
		rospy.init_node('pool_patrol', anonymous=False)

		# Set the shutdown fuction (stop the robot)
		rospy.on_shutdown(self.shutdown)

		# Initilalize the mission parameters and variables
		setup_task_environment(self)

		# Turn the target locations into SMACH MoveBase and LosPathFollowing action states
		nav_terminal_states = {}
		nav_transit_states = {}

		# DP controller
		for target in self.pool_locations.iterkeys():
			nav_goal = MoveBaseGoal()
			nav_goal.target_pose.header.frame_id = 'odom'
			nav_goal.target_pose.pose = self.pool_locations[target]
			move_base_state = SimpleActionState('move_base', MoveBaseAction,
												goal=nav_goal, 
												result_cb=self.nav_result_cb,
												exec_timeout=self.nav_timeout,
												server_wait_timeout=rospy.Duration(10.0))

			nav_terminal_states[target] = move_base_state

		# Path following
		for target in self.pool_locations.iterkeys():
			nav_goal = LosPathFollowingGoal()
			#nav_goal.prev_waypoint = navigation.vehicle_pose.position
			nav_goal.next_waypoint = self.pool_locations[target].position
			nav_goal.forward_speed.linear.x = self.transit_speed
			nav_goal.desired_depth.z = self.search_depth
			nav_goal.sphereOfAcceptance = self.search_area_size
			los_path_state = SimpleActionState('los_path', LosPathFollowingAction,
												goal=nav_goal, 
												result_cb=self.nav_result_cb,
												exec_timeout=self.nav_timeout,
												server_wait_timeout=rospy.Duration(10.0))

			nav_transit_states[target] = los_path_state

		""" Create individual state machines for assigning tasks to each target zone """

		# Create a state machine container for the orienting towards the gate subtask(s)
		sm_gate_tasks = StateMachine(outcomes=['found','unseen','missed','passed','aborted','preempted'])

		# Then add the subtask(s)
		with sm_gate_tasks:
			# if gate is found, pass pixel info onto TrackTarget. If gate is not found, look again
			StateMachine.add('SCANNING_OBJECTS', SearchForTarget('gate'), transitions={'found':'CAMERA_CENTERING','unseen':'BROADEN_SEARCH','passed':'','missed':''},
																	 remapping={'px_output':'object_px','fx_output':'object_fx','search_output':'object_search','search_confidence_output':'object_confidence'})

			StateMachine.add('CAMERA_CENTERING', TrackTarget('gate', self.pool_locations['gate'].position), transitions={'succeeded':'SCANNING_OBJECTS'},
														  remapping={'px_input':'object_px','fx_input':'object_fx','search_input':'object_search','search_confidence_input':'object_confidence'})

			StateMachine.add('BROADEN_SEARCH', TrackTarget('gate', self.pool_locations['gate'].position), transitions={'succeeded':'SCANNING_OBJECTS'},
														   remapping={'px_input':'object_px','fx_input':'object_fx','search_input':'object_search','search_confidence_input':'object_confidence'})


		# Create a state machine container for returning to dock
		sm_docking = StateMachine(outcomes=['succeeded','aborted','preempted'])

		# Add states to container

		with sm_docking:

			StateMachine.add('RETURN_TO_DOCK', nav_transit_states['docking'], transitions={'succeeded':'DOCKING_SECTOR','aborted':'','preempted':'RETURN_TO_DOCK'})
			StateMachine.add('DOCKING_SECTOR', ControlMode(POSE_HEADING_HOLD), transitions={'succeeded':'DOCKING_PROCEEDURE','aborted':'','preempted':''})
			StateMachine.add('DOCKING_PROCEEDURE', nav_terminal_states['docking'], transitions={'succeeded':'','aborted':'','preempted':''})

		""" Assemble a Hierarchical State Machine """

		# Initialize the HSM
		hsm_pool_patrol = StateMachine(outcomes=['succeeded','aborted','preempted','passed','missed','unseen','found'])

		# Build the HSM from nav states and target states

		with hsm_pool_patrol:

			""" Navigate to GATE in TERMINAL mode """
			StateMachine.add('TRANSIT_TO_GATE', nav_transit_states['gate'], transitions={'succeeded':'GATE_SEARCH','aborted':'DOCKING','preempted':'DOCKING'})

			""" When in GATE sector"""		
			StateMachine.add('GATE_SEARCH', sm_gate_tasks, transitions={'passed':'GATE_PASSED','missed':'DOCKING','aborted':'DOCKING'})		
			
			""" Transiting to gate """
			StateMachine.add('GATE_PASSED', ControlMode(OPEN_LOOP), transitions={'succeeded':'TRANSIT_TO_POLE','aborted':'DOCKING','preempted':'DOCKING'})
			StateMachine.add('TRANSIT_TO_POLE', nav_transit_states['pole'], transitions={'succeeded':'DOCKING','aborted':'DOCKING','preempted':'DOCKING'})

			""" When in POLE sector"""		
			#StateMachine.add('POLE_PASSING_TASK', sm_pole_tasks, transitions={'passed':'POLE_PASSING_TASK','missed':'RETURN_TO_DOCK','aborted':'RETURN_TO_DOCK'})		

			""" When aborted, return to docking """
			StateMachine.add('DOCKING', sm_docking, transitions={'succeeded':'','aborted':'','preempted':''})


		# Create and start the SMACH Introspection server

		intro_server = IntrospectionServer(str(rospy.get_name()),hsm_pool_patrol,'/SM_ROOT')
		intro_server.start()

		# Execute the state machine
		hsm_outcome = hsm_pool_patrol.execute()
		intro_server.stop()

	def nav_result_cb(self, userdata, status, result):

		if status == GoalStatus.PREEMPTED:
			rospy.loginfo("Waypoint preempted")
		if status == GoalStatus.SUCCEEDED:
			rospy.loginfo("Waypoint succeeded")

	def shutdown(self):
		rospy.loginfo("stopping the AUV...")
		#sm_nav.request_preempt()
		rospy.sleep(10)


if __name__ == '__main__':

	try:
		TaskManager()
	except rospy.ROSInterruptException:
		rospy.loginfo("Mission pool patrol has been finished")