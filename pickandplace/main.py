# Copyright 2024 @With-Robot 3rd
#
# Licensed under the MIT License;
#     https://opensource.org/license/mit

import cv2
import numpy as np

from pynput import keyboard
from pynput.keyboard import Listener

from coppeliasim_zmqremoteapi_client import RemoteAPIClient

from util import State, Context, Mission, ReadData, ControlData
from car import (
    get_location,
    move_to_pick,
    approach_to_target,
    move_to_place,
    move_to_base,
)
from manipulator import find_target, pick_target, place_target


#
# A class for the entire pick-and-place operation
#
class PickAndPlace:
    def __init__(self):
        # coppeliasim simulation instance
        self.sim = RemoteAPIClient().require("sim")
        # Context of Robot
        self.context = Context()
        # Simulation run flag
        self.run_flag = True

    # key event listener
    def on_press(self, key):
        # Pressing 'a' key will start the mission.
        if key == keyboard.KeyCode.from_char("a"):
            self.context.mission = Mission("A", "B", "Cube")

        # Pressing 'q' key will terminate the simulation.
        if key == keyboard.KeyCode.from_char("q"):
            self.run_flag = False

    # init coppeliasim objects
    def init_coppelia(self):
        # reference
        self.youBot_ref = self.sim.getObject("/youBot_ref")
        # Wheel Joints: front left, rear left, rear right, front right
        self.wheels = []
        self.wheels.append(self.sim.getObject("/rollingJoint_fl"))
        self.wheels.append(self.sim.getObject("/rollingJoint_rl"))
        self.wheels.append(self.sim.getObject("/rollingJoint_fr"))
        self.wheels.append(self.sim.getObject("/rollingJoint_rr"))

        # lidar
        self.lidars = []
        for i in range(13):
            self.lidars.append(self.sim.getObject(f"/lidar_{i+1:02d}"))

        # manipulator 5 joints
        self.joints = []
        for i in range(5):
            self.joints.append(self.sim.getObject(f"/youBotArmJoint{i}"))

        # Gripper Joint
        self.joints.append(self.sim.getObject(f"/youBotGripperJoint1"))
        self.joints.append(self.sim.getObject(f"/youBotGripperJoint2"))

        # camera
        self.camera_1 = self.sim.getObject(f"/camera_1")

        # joint & wheel control mode
        self.set_joint_ctrl_mode(self.wheels, self.sim.jointdynctrl_velocity)
        self.set_joint_ctrl_mode(self.joints, self.sim.jointdynctrl_position)

    # set joints dynamic control mode
    def set_joint_ctrl_mode(self, objects, ctrl_mode):
        for obj in objects:
            self.sim.setObjectInt32Param(
                obj,
                self.sim.jointintparam_dynctrlmode,
                ctrl_mode,
            )

    # read youbot data
    def read_youbot(self, lidar=False, camera=False):
        read_data = ReadData()
        # read localization of youbot
        p = self.sim.getObjectPosition(self.youBot_ref)
        o = self.sim.getObjectQuaternion(self.youBot_ref)
        read_data.localization = np.array(p + o)
        # read wheel joints
        wheels = []
        for wheel in self.wheels:
            theta = self.sim.getJointPosition(wheel)
            wheels.append(theta)
        read_data.wheels = wheels
        # read manipulator joints
        joints = []
        for joint in self.joints:
            theta = self.sim.getJointPosition(joint)
            joints.append(theta)
        read_data.joints = joints
        # read lidar
        if lidar:
            scans = []
            for id in self.lidars:
                scans.append(self.sim.readProximitySensor(id))
            read_data.scans = scans
        # read camera
        if camera:
            result = self.sim.getVisionSensorImg(self.camera_1)
            img = np.frombuffer(result[0], dtype=np.uint8)
            img = img.reshape((result[1][1], result[1][0], 3))
            img = cv2.flip(img, 1)
            read_data.img = img
        # return read_data
        return read_data

    # control youbot (return true if control is finished)
    def control_youbot(self, control_data):
        control_data.exec_count += 1

        if control_data.wheels_velocity is not None:
            return True
        if control_data.wheels_position is not None:
            return True
        if control_data.joints_position is not None:
            is_lidar = control_data.lidar_call_back is not None
            is_camera = control_data.camera_call_back is not None
            read_data = self.read_youbot(lidar=is_lidar, camera=is_camera)

            diff_sum = 0
            for i, joint in enumerate(control_data.joints_position):
                diff = abs(joint - read_data.joints[i])
                diff_sum += diff
                diff = min(diff, control_data.delta)
                if read_data.joints[i] < joint:
                    target = read_data.joints[i] + diff
                else:
                    target = read_data.joints[i] - diff
                self.sim.setJointTargetPosition(self.joints[i], target)
            if control_data.lidar_call_back:
                return control_data.lidar_call_back(
                    self.context, read_data, control_data
                )
            elif control_data.camera_call_back:
                return control_data.camera_call_back(
                    self.context, read_data, control_data
                )
            else:
                return diff_sum < 0.005
        if control_data.gripper_state is not None:
            p1 = self.sim.getJointPosition(self.joints[-2])
            p2 = self.sim.getJointPosition(self.joints[-1])
            p1 += -0.005 if control_data.gripper_state else 0.005
            p2 += 0.005 if control_data.gripper_state else -0.005
            self.sim.setJointTargetPosition(self.joints[-2], p1)
            self.sim.setJointTargetPosition(self.joints[-1], p2)
            return control_data.exec_count > 5
        return True

    # run coppeliasim simulator
    def run_coppelia(self):
        # Registering a Keyboard Listener
        Listener(on_press=self.on_press).start()
        # Start Simulation
        self.sim.setStepping(True)
        self.sim.startSimulation()

        # data of control
        control_data = None

        # Execution of the simulation
        while self.run_flag:
            if control_data:  # if control data exists complete control
                if self.control_youbot(control_data):
                    control_data = None
                self.sim.step()
                continue
            print(self.context.state)

            self.context.inc_state_counter()

            if self.context.state == State.StandBy:
                if self.context.mission != None:
                    self.context.set_state(State.MoveToPick)
                    base = get_location(self.context, self.read_youbot(lidar=True))
                    self.context.base = base
            elif self.context.state == State.MoveToPick:
                if self.context.state_counter == 1:
                    self.set_joint_ctrl_mode(
                        self.wheels, self.sim.jointdynctrl_velocity
                    )
                result, control_data = move_to_pick(
                    self.context, self.read_youbot(lidar=True)
                )
                if result:
                    self.context.set_state(State.FindTarget)
            elif self.context.state == State.FindTarget:
                if self.context.state_counter == 1:
                    self.set_joint_ctrl_mode(
                        self.wheels, self.sim.jointdynctrl_position
                    )
                result, control_data = find_target(self.context, self.read_youbot())
                if result:
                    self.context.set_state(State.ApproachToTarget)
            elif self.context.state == State.ApproachToTarget:
                result, control_data = approach_to_target(
                    self.context, self.read_youbot(lidar=True)
                )
                if result:
                    self.context.set_state(State.PickTarget)
            elif self.context.state == State.PickTarget:
                result, control_data = pick_target(
                    self.context, self.read_youbot(camera=True)
                )
                if result:
                    self.context.set_state(State.MoveToPlace)
            elif self.context.state == State.MoveToPlace:
                if self.context.state_counter == 1:
                    self.set_joint_ctrl_mode(
                        self.wheels, self.sim.jointdynctrl_velocity
                    )
                result, control_data = move_to_place(
                    self.context, self.read_youbot(lidar=True)
                )
                if result:
                    self.context.set_state(State.PlaceTarget)
            elif self.context.state == State.PlaceTarget:
                if self.context.state_counter == 1:
                    self.set_joint_ctrl_mode(
                        self.wheels, self.sim.jointdynctrl_position
                    )
                result, control_data = place_target(
                    self.context, self.read_youbot(camera=True)
                )
                if result:
                    self.context.set_state(State.MoveToBase)
            elif self.context.state == State.MoveToBase:
                if self.context.state_counter == 1:
                    self.set_joint_ctrl_mode(
                        self.wheels, self.sim.jointdynctrl_velocity
                    )
                result, control_data = move_to_base(
                    self.context, self.read_youbot(lidar=True)
                )
                if result:
                    self.context.set_state(State.StandBy)
                    self.context.mission = None  # clear mission

            # Run Simulation Step
            self.sim.step()

        # Stop Simulation
        self.sim.stopSimulation()


if __name__ == "__main__":
    client = PickAndPlace()
    client.init_coppelia()
    client.run_coppelia()