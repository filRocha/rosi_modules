#!/usr/bin/env python3
''' This is a ROSI algorithm
It controls the chassis
based on different control strategies
'''
import rospy
import numpy as np
import quaternion
from dqrobotics import *

from rosi_common.dq_tools import quat2rpy, rpy2quat, trAndOri2dq, dqElementwiseMul, dq2trAndQuatArray, dqExtractTransV3, dq2rpy
from rosi_common.rosi_tools import correctFlippersJointSignal, compute_J_ori_dagger, compute_J_art_dagger
from rosi_common.node_status_tools import nodeStatus
from rosi_model.rosi_description import tr_base_piFlp

from rosi_common.msg import Float32Array, Vector3ArrayStamped, DualQuaternionStamped
from sensor_msgs.msg import Imu, JointState
from geometry_msgs.msg import Vector3

from rosi_common.srv import SetNodeStatus, GetNodeStatusList, setPoseSetPointVec, setPoseCtrlGain, getPoseCtrlGain, getPoseSetPointVec, SetInt, SetIntResponse, GetInt, GetIntResponse


class NodeClass():

    def __init__(self, node_name):
        '''Class constructor'''
        self.node_name = node_name

        ##==== PARAMETERS ============================================

        #----------------- SET-POINT ------------------------------
        # orientation
        x_sp_ori_rpy = np.deg2rad([0, 0, 0]) # final vector is in radians RPY
        
        # ground distance
        x_sp_tr = [0.0, 0.0, 0.3] # in meters
       

        #----------------- CONTROL GAINS ------------------------------
        # orientation controller gain per DOF
        kp_rot_v = [2.0, 4.0, 1.0]

        # translation control gain per DOF
        kp_tr_v = [1.0, 1.0, 0.8]


        #------ Mu function for the null-space controller parameters
        # propulsion joints angular set-point for the null-space
        self.flpJointPosSp_l = 4*[np.deg2rad(110)]

        # mu function gain
        self.kmu_l = 4*[0.3]

        #---- Divers

        # rosi direction side
        self.drive_side_param_path = '/rosi/forward_side'
        self.drive_side = self.getParamWithWait(self.drive_side_param_path)

    
        ##=== Useful variables
        # node status object
        self.ns = nodeStatus(node_name)
        #self.ns.resetActive() # this node is disabled by default

        # ROS topic message variables
        self.msg_grndDist = None
        self.msg_jointState = None
        self.msg_imu = None

        # Available control types
        self.chassisCtrlType = {
            "orientation": 1,
            "orientationNullSpace": 2,
            "articulation": 3
        }

        # current control type
        self.ctrlType_curr = self.chassisCtrlType['orientation']

        # for storing joints and w function last values
        self.last_jointPos = None
        self.last_f_w = None


        ##========= One-time calculations ===================================
        # computing the set-points in orientation quaternion and pose dual quaternion
        self.x_sp_ori_q, self.x_sp_dq  = self.convertSetPoints2DqFormat(x_sp_tr, x_sp_ori_rpy)

        # orientation controller gain in quaternion format
        self.kp_o_q = np.quaternion(1, kp_rot_v[0], kp_rot_v[1], kp_rot_v[2])

        # articulation  controller gain in dual quaternion format
        self.kp_a_dq = DQ(1, kp_rot_v[0], kp_rot_v[1], kp_rot_v[2], 1, kp_tr_v[0], kp_tr_v[1], kp_tr_v[2])  

        # chassis orientation kinematics considering that propulsion and chassis frames have all the same orientation (identity matrix)
        self.J_ori_dagger = compute_J_ori_dagger(tr_base_piFlp.values())

        # the orientation jacobian
        self.J_ori = np.linalg.pinv(self.J_ori_dagger)

        # the orientation jacobian null-space projector
        self.J_ori_nsproj = np.eye(4) - np.dot(self.J_ori_dagger, self.J_ori)

        # chassis articulation kinematics considering that propulsion and chassis frames have all the same orientation (identity matrix)
        self.J_art_dagger = compute_J_art_dagger(tr_base_piFlp.values())


        ##==== ROS interfaces
        # publishers
        self.pub_cmdVelFlipperSpace = rospy.Publisher('/rosi/flippers/space/cmd_v_z/leveler', Vector3ArrayStamped, queue_size=5)

        self.pub_dqPoseCurr = rospy.Publisher('/chassis_control/pose_current', DualQuaternionStamped, queue_size=5)
        self.pub_dqSetPoint = rospy.Publisher('/chassis_control/pose_sp', DualQuaternionStamped, queue_size=5)
        self.pub_dqError = rospy.Publisher('/chassis_control/pose_error', DualQuaternionStamped, queue_size=5)
        self.pub_dqSP = rospy.Publisher('/chassis_control/sp_dq', DualQuaternionStamped, queue_size=5)

        # subscribers
        sub_imu = rospy.Subscriber('/sensor/imu_corrected', Imu, self.cllbck_imu)
        sub_grndDist = rospy.Subscriber('/rosi/model/base_ground_distance', Vector3ArrayStamped, self.cllbck_grndDist)
        sub_jointState = rospy.Subscriber('/rosi/rosi_controller/joint_state', JointState, self.cllbck_jointState)

        # services
        srv_setActive = rospy.Service(self.ns.getSrvPath('active', rospy), SetNodeStatus, self.srvcllbck_setActive)
        srv_getStatus = rospy.Service(self.ns.getSrvPath('getNodeStatus', rospy), GetNodeStatusList, self.srvcllbck_getStatus)
        srv_setHaltCmd = rospy.Service(self.ns.getSrvPath('haltcmd', rospy), SetNodeStatus, self.srvcllbck_setHaltCmd)
        
        srv_setPoseSetPoint = rospy.Service(self.node_name+'/set_pose_set_point', setPoseSetPointVec, self.srvcllbck_setPoseSetPoint) 
        srv_setPoseCtrlGain = rospy.Service(self.node_name+'/set_pose_ctrl_gain', setPoseCtrlGain, self.srvcllbck_setPoseCtrlGain) 
        srv_setCtrlType = rospy.Service(self.node_name+'/set_ctrl_type', SetInt, self.srvcllbck_setCtrlType) 

        srv_getPoseSetPoint = rospy.Service(self.node_name+'/get_pose_set_point', getPoseSetPointVec, self.srvcllbck_getPoseSetPoint) 
        srv_getPoseCtrlGain = rospy.Service(self.node_name+'/get_pose_ctrl_gain', getPoseCtrlGain, self.srvcllbck_getPoseCtrlGain) 
        srv_getCtrlType = rospy.Service(self.node_name+'/get_ctrl_type', GetInt, self.srvcllbck_getCtrlType)

        # Node main
        self.nodeMain()

    
    def nodeMain(self):
        '''Node main method'''

        # defining the eternal loop rate
        node_rate_sleep = rospy.Rate(20)

        rospy.loginfo('[%s] Entering in ethernal loop.', self.node_name)
        while not rospy.is_shutdown():

            # only runs if node is active
            if self.ns.getNodeStatus()['active']: 

                # only runs the control when all needed input variables are available
                if self.msg_grndDist is not None and self.msg_jointState is not None and self.msg_imu is not None:

                    #=== Required computations independently of the current control mode
                    # setting the imu ROS data in the numpy quaternion format
                    q_imu = np.quaternion(self.msg_imu.orientation.w, self.msg_imu.orientation.x, self.msg_imu.orientation.y, self.msg_imu.orientation.z)

                    # computing the chassis orientation state without the yaw component
                    rpy = quat2rpy(q_imu)
                    q_yawcorr = rpy2quat([0, 0, rpy[2]])
                    q_yawcorr = np.quaternion(q_yawcorr[0], q_yawcorr[1], q_yawcorr[2], q_yawcorr[3])
                    x_o_R_q = q_imu * q_yawcorr

                    # defining the articulation pose state
                    x_a_R_dq = trAndOri2dq([0, 0, self.msg_grndDist.vec[0].z], x_o_R_q, 'trfirst')


                    #=== Control modes implementation
                    # If the control mode is orientation
                    if self.ctrlType_curr == self.chassisCtrlType['orientation'] or self.ctrlType_curr == self.chassisCtrlType['orientationNullSpace']:
                        
                        # computing the orientation error
                        e_o_R_q = self.x_sp_ori_q.conj() * x_o_R_q

                        # control signal component due to the orientation error
                        u_o_R_q = np.multiply(self.kp_o_q.conj().components, e_o_R_q.components)
                        u_o_R_v = np.array([u_o_R_q[1], u_o_R_q[2]]).reshape(2,1)
                        u_Pi_v =  np.dot(self.J_ori_dagger, u_o_R_v)

                        # if the joints optimization is enabled, computes the null space component if this control mode is enabled
                        if self.ctrlType_curr == self.chassisCtrlType['orientationNullSpace']:

                            # treating flippers position
                            flpJPos_l = correctFlippersJointSignal(self.msg_jointState.position[4:])

                            # computing the null-space projector function component
                            mu = np.array([ ki * (jsp - jcurr) for jcurr, jsp, ki in zip(flpJPos_l, self.flpJointPosSp_l, self.kmu_l)]).reshape(4,1)

                            # computing the null-space projector control signal component
                            aux_u = np.dot(self.J_ori_nsproj, mu)

                            # summing the component to the current orientation signal
                            u_Pi_v = u_Pi_v + aux_u


                    # If the control mode is articulation
                    elif self.ctrlType_curr == self.chassisCtrlType['articulation']:

                        # computing the pose error
                        e_a_R_dq = self.x_sp_dq .conj() * x_a_R_dq

                        # computing articulation the control signal
                        u_a_R_dq = dqElementwiseMul(self.kp_a_dq.conj(), e_a_R_dq) # kp_dq.conj

                        # converting the control signal to vector (translation) and quaternion (orientation) formats
                        u_a_R_tr, u_a_R_q = dq2trAndQuatArray(u_a_R_dq)

                        # defining the control signal vector
                        u_a_R = np.array([u_a_R_tr[2][0], u_a_R_q.components[1], u_a_R_q.components[2]]).reshape(3,1)

                        # control signal for each propulsion mechanisms vertical axis
                        u_Pi_v = np.dot(self.J_art_dagger, u_a_R)
     

                    # If the selected control mode is invalid
                    else:
                        u_Pi_v = np.array([0, 0, 0, 0]).reshape(4,1)
                    

                    #=== Publishing the ROS message for the controller
                    # receiving ROS time
                    ros_time = rospy.get_rostime()

                    # updates drive param
                    self.drive_side = rospy.get_param(self.drive_side_param_path)

                    # mounting and publishing
                    m = Vector3ArrayStamped()
                    m.header.stamp = ros_time
                    m.header.frame_id = self.node_name
                    m.vec = [Vector3(0, 0, u_Pi[0]) for u_Pi in u_Pi_v]
                    self.pub_cmdVelFlipperSpace.publish(m)     


                    #=== Publishing control metrics
                    #current pose
                    m = self.dq2DualQuaternionStampedMsg(x_a_R_dq, ros_time, self.node_name)
                    self.pub_dqPoseCurr.publish(m)

                    # pose set-point
                    m = self.dq2DualQuaternionStampedMsg(self.x_sp_dq, ros_time, self.node_name)
                    self.pub_dqSetPoint.publish(m)

                    # pose error
                    if self.ctrlType_curr == self.chassisCtrlType['orientation'] or self.ctrlType_curr == self.chassisCtrlType['orientationNullSpace']:
                        # creates the dual quaternion error if it still does not exists (in case of orientation control)
                        aux = e_o_R_q.components
                        e_a_R_dq = DQ(aux[0], aux[1], aux[2], aux[3], 0, 0, 0, 0)
                    m = self.dq2DualQuaternionStampedMsg(e_a_R_dq, ros_time, self.node_name)
                    self.pub_dqError.publish(m)

                    # controller gain
                    m = self.dq2DualQuaternionStampedMsg(self.kp_a_dq , ros_time, self.node_name)
                    self.pub_dqSP.publish(m)

            # sleeping the node
            node_rate_sleep.sleep()

    
    ''' === Topics callbacks ==='''
    def cllbck_imu(self, msg):
        '''Callback for the IMU messages.'''
        self.msg_imu = msg      


    def cllbck_grndDist(self, msg):
        '''Callback for received distance to the ground info'''
        # stores received distance to the ground as a 3D vector
        self.msg_grndDist = msg

    
    def cllbck_jointState(self, msg):
        ''' Callback for flippers state'''
        self.msg_jointState = msg
    

    ''' === Service Callbacks === '''
    def srvcllbck_setActive(self, req):
        ''' Method for setting the active node status flag'''
        return self.ns.defActiveServiceReq(req, rospy)


    def srvcllbck_getStatus(self, req):
        ''' Method for returning the node status flag list'''
        return self.ns.getNodeStatusSrvResponse()
    
    
    def srvcllbck_setHaltCmd(self, req):
        ''' Method for setting the haltCmd node status flag'''
        return self.ns.defHaltCmdServiceReq(req, rospy)    


    def srvcllbck_setPoseSetPoint(self, req):
        ''' Service callback method for redefining the pose set-point given 
        two 3D vectors (translation [m] + orientation in euler-RPY [rad]'''
        # updating set-point variables
        self.x_sp_ori_q, self.x_sp_dq  = self.convertSetPoints2DqFormat(list(req.translation), list(req.orientation))
        return True


    def srvcllbck_setPoseCtrlGain(self, req):
        '''Service that sets the controller gains'''

        # orientation controller gain in quaternion format
        self.kp_o_q = np.quaternion(1, req.kp_ori[0], req.kp_ori[1], req.kp_ori[2])

        # articulation  controller gain in dual quaternion format
        self.kp_a_dq = DQ(1, req.kp_ori[0], req.kp_ori[1], req.kp_ori[2], 1, req.kp_tr[0], req.kp_tr[1], req.kp_tr[2])  

        return True
    

    def srvcllbck_getPoseSetPoint(self, req):
        ''' Service callback method for retrieving the pose set-point given 
        two 3D vectors (translation + orientation in euler-XYZ'''
        # setting new set-point
        return [dqExtractTransV3(self.x_sp_dq ).reshape(1,3).tolist()[0] ,dq2rpy(self.x_sp_dq)]
    

    def srvcllbck_getPoseCtrlGain(self, req):
        '''Service that gets controller gains'''
        aux = self.kp_a_dq.vec8().tolist()
        return [aux[1:4], aux[5:]]
    

    def srvcllbck_setCtrlType(self, req):
        ''' Callback for changing current control type'''
        # preparing the response
        resp = SetIntResponse()

        # confirming if the requested control type is a valid one
        if req.value >= 1 and req.value <= len(self.chassisCtrlType):
            self.ctrlType_curr = req.value
            resp.ret = self.ctrlType_curr 
            rospy.loginfo('[%s] Setting control type to: %s.', self.node_name, self.get_key_by_value(self.chassisCtrlType, self.ctrlType_curr))

        # in case of the received control mode is unavailable
        else:
            resp.ret = -1
            rospy.logerr('[%s] Received a bad control type: %s.', self.node_name, req.value)

        return resp


    def srvcllbck_getCtrlType(self, req):
        ''' Callback to inform the current control type'''
        ret = GetIntResponse()
        ret.ret = int(self.ctrlType_curr)
        return ret
       

    #=== Static methods
    @staticmethod
    def getParamWithWait(path_param):
        """Waits until a param exists so retrieves it"""
        while not  rospy.has_param(path_param):
            rospy.loginfo("[manager] Waiting for param: %s", path_param)
        return rospy.get_param(path_param)


    @staticmethod
    def convertSetPoints2DqFormat(tr, rpy):
        '''Converts two arrays containing translation and orientation set-points to the orientation quaternion
        and pose dual quaternion formats. Useful for defining and updating the set-points
        Input
            - tr <list/np.array>: the translation set-point vector
            - rpy <list/np.array>: the orientation set-point in Roll Pitch Yaw format
        Output
            - ori_q <np.quaternion>: the orientation set-point in quaternion format
            - pose_dq <dqrobotics.DQ>: the pose set-point in dual quaternion format   
        '''
        
        # orientation in quaternion format
        ori_q = rpy2quat(rpy)
        ori_q = np.quaternion(ori_q[0], ori_q[1], ori_q[2], ori_q[3])

        # pose dual quaternion
        pose_dq = trAndOri2dq(tr, ori_q, 'trfirst')

        return ori_q, pose_dq


    @staticmethod
    def get_key_by_value(dictionary, value):
        ''' Gets the key of a dictionary given a value'''
        for key, val in dictionary.items():
            if val == value:
                return key
        return None  # Value not found


    @staticmethod
    def dq2DualQuaternionStampedMsg(dq, ros_time, frame_id):
        '''Converts a dual-quaternion variable into a ROS DualQuaternionStamped message
        Input
            - dq<DQ>: the dual quaternion variable
        Output
            - an object <DualQuaternionStamped> as a ROS message.'''
        aux = dq.vec8()
        m = DualQuaternionStamped()
        m.header.stamp = ros_time
        m.header.frame_id = frame_id
        m.wp = aux[0]
        m.xp = aux[1]
        m.yp = aux[2]
        m.zp = aux[3]
        m.wd = aux[4]
        m.xd = aux[5]
        m.yd = aux[6]
        m.zd = aux[7]
        return m


if __name__ == '__main__':
    node_name = 'chassis_control'
    rospy.init_node(node_name, anonymous=True)
    rospy.loginfo('node '+node_name+' initiated.')
    rospy.loginfo('Actually, articulationC_control_1 node initiated!!! Testing purposes only.')
    try:
        node_obj = NodeClass(node_name)
    except rospy.ROSInternalException: pass



    