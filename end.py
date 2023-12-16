import heapq
import random
import time
import uuid
import threading
from datetime import datetime, timedelta

from sqlalchemy import Column, Integer, String, Enum, ForeignKey, DateTime, Float
from sqlalchemy.orm import relationship

from utils.enums import Role, FanSpeed, AcMode, QueueState

import os
from flask import Flask, abort, request, jsonify, render_template, redirect, url_for, session, Blueprint, send_file
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import pandas as pd
import requests
import json

TIME_EXPIRES = 7  # 7days
app = Flask(__name__)
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SECRET_KEY'] = os.urandom(24)
CORS(app)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///hotel.db'
db = SQLAlchemy(app)


class ACScheduler:
    def __init__(self, db, interval=1):
        # 初始化空调调度器
        self.db = db  # 数据库连接
        self.interval = interval  # 调度更新时间间隔（以秒为单位）
        self.max_num = 3  # 最大同时运行的空调数量
        self.running_list = []  # 正在运行的空调列表
        self.waiting_queue = []  # 等待队列，存放等待运行的空调
        self.last_update = time.time()  # 上次调度更新时间
        self.cooling_rate = 0.5 / 60  # 房间回温速率（每分钟）
        self.rate = 1.  # 空调费率（每单位温度改变的费用）

        self.boost = 6.  # 空调性能提升系数

    def minimum(self, a1, a2):
        # 返回两个数的最小值和索引（0表示第一个数最小，1表示第二个数最小）
        return (a1, 0) if a1 < a2 else (a2, 1)

    def get_speed(self, fanSpeed):
        # 根据风扇速度返回每分钟的温度改变速率
        if fanSpeed == FanSpeed.HIGH:
            return 1.
        elif fanSpeed == FanSpeed.MEDIUM:
            return 0.5
        else:
            return 1 / 3

    def get_priority(self, acSpeed):
        # 根据空调速度返回优先级（高速度优先级最低）
        return {'high': 1, 'medium': 2, 'low': 3}.get(acSpeed.value, 3)

    def add_to_waiting(self, room):
        # 将房间添加到等待队列中
        room.queueState = QueueState.PENDING
        db.session.commit()
        # 将房间信息添加到等待队列，并根据优先级和随机值排序
        self.waiting_queue = [(priority, random.random(), room_id) for priority, random_val, room_id in
                              self.waiting_queue]
        heapq.heappush(self.waiting_queue, (self.get_priority(room.fanSpeed), random.random(), room.roomID))
        if room.roomID in self.running_list:
            self.running_list.remove(room.roomID)
        # 在这里产生详单记录（未提供代码示例）

    def update(self):
        # 更新空调调度状态
        with app.app_context():
            t = time.time()

            rooms = self.db.session.query(Room).all()
            for room in rooms:
                if room.queueState == QueueState.RUNNING:  # 空调开启时
                    if room.roomTemperature > room.acTemperature:  # 制冷
                        delta, argmin = self.minimum(room.roomTemperature - room.acTemperature,
                                                     self.get_speed(room.fanSpeed) * (
                                                             t - self.last_update) / 60 * self.boost)
                        if argmin == 0:
                            self.add_to_waiting(room)
                        room.roomTemperature -= delta
                        room.consumption += delta * self.rate
                        db.session.commit()
                    elif room.roomTemperature < room.acTemperature:  # 制热
                        delta, argmin = self.minimum(room.acTemperature - room.roomTemperature,
                                                     self.get_speed(room.fanSpeed) * (
                                                             t - self.last_update) / 60 * self.boost)
                        if argmin == 0:
                            self.add_to_waiting(room)
                        room.roomTemperature += delta
                        room.consumption += delta * self.rate
                        db.session.commit()
                    else:
                        self.add_to_waiting(room)  # 达到目标温度回到等待队列
            # print(self.running_list, self.waiting_queue)

            for roomID in self.running_list:
                room = db.session.query(Room).filter_by(roomID=roomID).one()
                # print(datetime.now() - room.firstRuntime)
                if datetime.now() - room.firstRuntime > timedelta(minutes=2) / self.boost:  # 2分钟 / 6（性能提升系数）
                    print('over time!')
                    self.add_to_waiting(room)

            for room in rooms:
                if room.queueState != QueueState.RUNNING:  # 空调关闭时
                    if room.roomTemperature > room.initialTemperature:  # 房间的回温逻辑
                        room.roomTemperature = max(
                            room.roomTemperature - self.cooling_rate * (t - self.last_update) * self.boost,
                            room.initialTemperature)
                    else:
                        room.roomTemperature = min(
                            room.roomTemperature + self.cooling_rate * (t - self.last_update) * self.boost,
                            room.initialTemperature)
                    db.session.commit()

            while self.waiting_queue and len(self.running_list) < self.max_num:
                _, _, roomID = heapq.heappop(self.waiting_queue)
                room = self.db.session.query(Room).filter_by(roomID=roomID).one()
                room.queueState = QueueState.RUNNING
                room.firstRuntime = datetime.now()
                db.session.commit()
                self.running_list.append(roomID)

            self.last_update = t

    def turn_off(self, room):
        # 将房间的状态从PENDING/RUNNING切换到IDLE（关闭空调）
        room.queueState = QueueState.IDLE
        db.session.commit()
        if room.roomID in self.running_list:
            self.running_list.remove(room.roomID)
        self.waiting_queue = [(priority, random_value, roomID) for priority, random_value, roomID in self.waiting_queue
                              if roomID != room.roomID]
        # 在这里产生详单记录（未提供代码示例）
        print('turn off!', room.queueState, self.running_list, self.waiting_queue)

    def turn_on(self, room):
        # 将房间的状态从IDLE切换到PENDING（打开空调）
        in_waiting = False
        for p, random_value, id, in self.waiting_queue:
            if id == room.roomID:
                in_waiting = True

        if room.roomID not in self.running_list and not in_waiting:
            self.add_to_waiting(room)
        print('turn on!', room.queueState, self.running_list, self.waiting_queue)

    def schedule_wrapper(self):
        try:
            self.update()  # 调用 调度函数
        finally:
            # 安排下一次执行
            threading.Timer(self.interval, self.schedule_wrapper).start()

    def start(self):
        threading.Timer(self.interval, self.schedule_wrapper).start()


scheduler = ACScheduler(db)
scheduler.start()


class Account(db.Model):
    __tablename__ = 'account'
    accountID = Column(Integer, primary_key=True)
    roomID = Column(Integer, ForeignKey('room.roomID'), nullable=True)

    username = Column(String, nullable=False, unique=True)
    password = Column(String, nullable=False)
    role = Column(Enum(Role), nullable=False)
    idCard = Column(String, nullable=True)
    phoneNumber = Column(String, nullable=True)

    createTime = Column(DateTime, nullable=False)
    room = relationship('Room', backref='accounts')

    def __init__(self, username: str, password: str, role: Role, roomID: int = None, idCard: str = None,
                 phoneNumber: str = None):
        """
        登记入住、创建共享帐号
        """
        self.username = username
        self.password = password
        self.role = role

        assert not (role == Role.customer and roomID is None), "客户帐号在创建时必须指定房间ID"

        self.roomID = roomID
        self.idCard = idCard
        self.phoneNumber = phoneNumber

        self.createTime = datetime.now()


class Room(db.Model):
    __tablename__ = 'room'
    roomID = Column(Integer, primary_key=True)
    roomName = Column(String, unique=True, nullable=False)
    unitPrice = Column(Float, nullable=False)
    roomDescription = Column(String)
    consumption = Column(Float)
    roomTemperature = Column(Float)
    acTemperature = Column(Integer)

    fanSpeed = Column(Enum(FanSpeed))
    acMode = Column(Enum(AcMode))
    initialTemperature = Column(Float)
    queueState = Column(Enum(QueueState))
    firstRuntime = Column(DateTime, nullable=True)  # 在被调度为RUNNING态时必须指定

    customerSessionID = Column(String, nullable=True)  # 在用户入住时必须指定
    checkInTime = Column(DateTime, nullable=True)  # 在用户入住时必须指定
    # 房间的全部记录，管理员可见
    # 用户可见的部分是与当前房间customerSessionID相同的部分
    # 也可以通过与身份证号相同的部分查看历史记录
    records = relationship('RoomRecord', backref='room')

    def __init__(self, roomName: str, roomDescription: str, unitPrice: float, acTemperature: int, fanSpeed: FanSpeed,
                 acMode: AcMode, initialTemperature: float = None):
        """
        创建房间
                >> room = Room('243', '大床房', acTemperature=30, fanSpeed=FanSpeed.MEDIUM, acMode=AcMode.HEAT)

        """

        self.roomName = roomName
        self.roomDescription = roomDescription
        self.unitPrice = unitPrice

        self.acTemperature = acTemperature  # 指定为管理员默认设置
        self.fanSpeed = fanSpeed  # 指定为管理员默认设置
        self.acMode = acMode  # 指定为管理员默认设置

        self.queueState = QueueState.IDLE

        self.initialTemperature = random.randint(15, 35) if initialTemperature is None else initialTemperature
        self.roomTemperature = self.initialTemperature

        self.consumption = 0.0
        self.firstRuntime = None
        self.customerSessionID = None


class RoomRecord(db.Model):
    __tablename__ = 'room_records'
    id = Column(Integer, primary_key=True)
    roomID = Column(Integer, ForeignKey('room.roomID'))
    customSessionID = Column(String)
    # 表会只保留过去3年的历史记录
    requestTime = Column(DateTime)
    serveStartTime = Column(DateTime)
    serveEndTime = Column(DateTime)
    fanSpeed = Column(String)
    acMode = Column(String)
    rate = Column(Float)
    consumption = Column(Float)
    accumulatedConsumption = Column(Float)

    def __init__(self, roomID, customerSessionID, requestTime, serveStartTime, serveEndTime, fanSpeed, acMode, rate,
                 consumption, accumulatedConsumption):
        self.roomID = roomID
        self.customerSessionID = customerSessionID
        self.requestTime = requestTime
        self.serveStartTime = serveStartTime
        self.serveEndTime = serveEndTime
        self.fanSpeed = fanSpeed
        self.acMode = acMode
        self.rate = rate
        self.consumption = consumption
        self.accumulatedConsumption = accumulatedConsumption


class Setting(db.Model):
    __tablename__ = 'settings'
    settingID = Column(Integer, primary_key=True)
    createTime = Column(DateTime)
    rate = Column(Float)
    defaultFanSpeed = Column(Enum(FanSpeed))
    defaultTemperature = Column(Integer)
    minTemperature = Column(Integer)
    maxTemperature = Column(Integer)
    acMode = Column(Enum(AcMode))

    def __init__(self, rate: float, defaultFanSpeed: FanSpeed, defaultTemperature: int, minTemperature: int,
                 maxTemperature: int, acMode: AcMode):
        self.rate = rate
        self.defaultFanSpeed = defaultFanSpeed
        self.defaultTemperature = defaultTemperature
        self.minTemperature = minTemperature
        self.maxTemperature = maxTemperature
        self.acMode = acMode

        self.createTime = datetime.now()


with app.app_context():
    db.create_all()

    # 检查并添加 Room
    existing_room = Room.query.filter_by(roomName='211').first()
    if not existing_room:
        room = Room('211', '大床房', 300, 25, FanSpeed.MEDIUM, AcMode.HEAT)
        db.session.add(room)
        db.session.commit()
    else:
        # 如果房间已存在，根据需要决定是更新还是跳过
        # 例如: room = existing_room
        pass

    # 检查并添加 Account
    existing_account = Account.query.filter_by(username='222').first()
    if not existing_account:
        account = Account('222', '222', Role.manager)
        db.session.add(account)
        db.session.commit()
    else:
        # 如果账户已存在，根据需要决定是更新还是跳过
        # 例如: account = existing_account
        pass

    # 检查并添加第二个 Account
    existing_account2 = Account.query.filter_by(username='111').first()
    if not existing_account2:
        account2 = Account('111', '111', Role.customer, room.roomID, '66666', '13w3252')
        db.session.add(account2)
    else:
        # 如果账户已存在，根据需要决定是更新还是跳过
        # 例如: account2 = existing_account2
        pass

    # 添加 Setting
    settings = Setting(1., FanSpeed.MEDIUM, 25, 16, 30, AcMode.HEAT)
    db.session.add(settings)
    db.session.commit()

def create_account(data, account_id):
    """
    [管理员，前台]
    create account(绑定帐号与房间关联) > check-in(额外多一个房间为空的判定)
    前台只能创建客户帐号（办理入住）
    管理员可以创建所有帐号
    # data
        # username
        # password
        # role (前台不可选，管理员可选)
        # idCard (前台必选，管理员可选)
        # phoneNumber (前台必选，管理员可选)
        # roomName (前台必选，管理员可选)
    :return:
    """
    origin_account = db.session.query(Account).filter_by(accountID=account_id).one()
    if origin_account.role == Role.customer:
        abort(401, "Unauthorized")  # 客户无权限访问该api

    if origin_account.role == Role.frontDesk and data.get('role'):  # 前台不能设定角色，只能创建客户帐号
        abort(401, "Unauthorized")

    role = Role.customer if origin_account.role == Role.frontDesk else Role[data['role']]

    if role == Role.customer and not data.get('roomName'):  # 必须为客户指定房间名
        abort(400, "roomName required")
    elif role != Role.customer and data.get('roomName'):
        abort(400, "roomName can only be allocated to customers")

    if role == Role.customer:
        room = db.session.query(Room).filter_by(roomName=data['roomName']).one_or_none()
        if room is None:
            abort(404, "room not found")
        if len(room.accounts) == 0:
            latest_settings = db.session.query(Setting).order_by(Setting.createTime.desc()).first()
            room.queueState = QueueState.IDLE
            room.fanSpeed = latest_settings.defaultFanSpeed
            room.acMode = latest_settings.acMode
            room.consumption = 0.0
            room.acTemperature = latest_settings.defaultTemperature
            room.customerSessionID = str(uuid.uuid4())
            room.checkInTime = datetime.now()
        else:
            abort(403, "room is occupied")
        room_id = room.roomID
    else:
        room_id = None

    try:
        new_account = Account(data['username'], data['password'], role, room_id, data.get('idCard'),
                              data.get('phoneNumber'))
        db.session.add(new_account)
        db.session.commit()
    except KeyError as error:
        abort(400, f'Bad request: {error}')

    return True


# @app.route('/accounts', methods=['GET'])
# @jwt_required()
# def get_accounts():
#     """
#     [管理员，前台]
#     获取所有帐号信息
#     管理员可以查看所有的
#     前台可以查看所有客户的
#     :return:
#     """
#     account_id = get_jwt_identity()
#     origin_role = db.session.query(Account).filter_by(accountID=account_id).one().role
#     if origin_role == Role.customer:
#         abort(401, "Unauthorized")
#     query = db.session.query(Account)
#     if origin_role == Role.frontDesk:
#         query = query.filter_by(role=Role.customer)  # 前台只能查询所有顾客帐号
#     accounts = query.all()
#     accounts_info = []
#     for a in accounts:
#         if a.role == Role.customer:
#             room = a.room
#             if room is None:
#                 abort(500, "found invalid customer whose room is invalid.")
#             accounts_info.append(dict(username=a.username, roomName=room.roomName, roomDescription=room.roomDescription,
#                                       createTime=a.createTime, checkInTime=room.checkInTime,
#                                       consumption=room.consumption,
#                                       role=a.role.value, idCard=a.idCard, phoneNumber=a.phoneNumber))
#         else:
#             accounts_info.append(dict(username=a.username, roomName=None, roomDescription=None,
#                                       createtime=a.createTime, checkInTime=None, consumption=None,
#                                       role=a.role.value, idCard=a.idCard, phoneNumber=a.phoneNumber))
#
#     return jsonify(accounts=accounts_info)
#
#
# @app.route('/account', methods=['GET', 'POST'])
# @app.route('/account/<string:username>', methods=['GET', 'POST'])
# @jwt_required()
# def account(username=None):
#     """
#     [客户，前台，管理员]
#     GET：
#     客户只能查看自己的
#     前台能查看客户和自己的
#     管理员能查看所有人的，也包括自己的
#     (不指定username参数查看自己的)
#     POST：
#     客户修改自己的帐号密码，需要旧的密码
#     前台修改客户和自己的帐号和密码，不需要旧的密码
#     管理员需要修改所有人的帐号和密码，不需要旧的密码
#     """
#     account_request = db.session.query(Account).filter_by(accountID=get_jwt_identity()).one()
#     role_request = account_request.role
#     if role_request == Role.customer and username is not None:
#         abort(403, "customers should not visit other accounts")
#
#     account = account_request if username is None else db.session.query(Account).filter_by(
#         username=username).one_or_none()
#     if account is None:
#         abort(404, "account not found")
#
#     if account.role != Role.customer and role_request == Role.frontDesk and username is not None:  # 前台只有权访问和修改客户的帐号和自己的帐号
#         abort(401, "Unauthorized")
#
#     if request.method == 'GET':
#         room = account.room
#         return jsonify(username=account.username, roomName=None if room is None else room.roomName,
#                        roomDescription=None if room is None else room.roomDescription,
#                        createTime=account.createTime, checkInTime=None if room is None else room.checkInTime,
#                        consumption=None if room is None else room.consumption,
#                        role=account.role.value, idCard=account.idCard, phoneNumber=account.phoneNumber)
#
#     elif request.method == 'POST':
#         data = request.json
#         if account_request.role == Role.customer:  # 客户修改自己的用户名和密码
#             if data.get('newUsername'):  # 修改用户名
#                 account.username = data['newUsername']
#             if data.get('password') and data.get('newPassword'):  # 修改密码，需要旧的密码验证
#                 if data['password'] != account.password:
#                     abort(403, "password incorrect")
#                 account.password = data['newPassword']
#         else:
#             if data.get('username'):  # 前台和管理员修改密码，不需要旧的密码验证
#                 account.username = data['username']
#             if data.get('password'):
#                 account.password = data['password']


def account_delete(data, token):
    """
    [管理员，前台]

    # data
        # roomName 退房+删除所有关联帐号，管理员和前台，只能房间名
        # username 帐号删除, 管理员，只能删非客户帐号
    :return:
    """
    origin_role = db.session.query(Account).filter_by(accountID=token).one().role
    if origin_role == Role.customer:
        abort(401, "Unauthorized")  # 客户无权访问

    if data.get('roomName'):  # 提供房间名，办理退房，管理员和前台可以用于办理退房
        room = db.session.query(Room).filter_by(roomName=data['roomName']).one_or_none()
        if room is None:
            abort(404, "room is already not in use")
        room.customerSessionID = None  # 退房流程
        room.checkInTime = None
        room.queueState = QueueState.IDLE
        room.consumption = 0.0
        for account in room.accounts:  # 删除所有关联帐号
            db.session.delete(account)
        db.session.commit()

    elif data.get('username'):  # 提供帐号，删除帐号，只有管理员能删除非客户帐号
        account = db.session.query(Account).filter_by(username=data['username']).one_or_none()
        if origin_role != Role.manager:
            abort(401, "Unauthorized")  # 除了管理员无法访问
        if account is None:
            abort(404, "username does not exists")
        if account.role == Role.customer:
            abort(403, "Please use checkout for customers")

        db.session.delete(account)
        db.session.commit()

    return True


def login(data):
    """
    parameters:
        - username
        - password
        - role

    responses:
        - token

    raise:
    :return:
    """
    try:
        role = Role[data['role']]
    except KeyError:
        return False
    result = db.session.query(Account).filter_by(username=data['username'], password=data['password'],
                                                 role=role).one_or_none()
    print(data)
    if result is None:
        return False
    return {'token': result.accountID}


@app.route('/room/create', methods=['POST'])
def room_create():
    """
    [管理员]
    管理员可以创建房间

    # data
        # roomName 房间名称
        # roomDescription 房间描述
        # unitPrice 房间单价

    :return:
    """
    print(request.json['token'])
    origin_role = db.session.query(Account).filter_by(username=request.json['token']).one().role
    if origin_role != Role.manager:
        abort(401, "Unauthorized")

    data = request.json
    latest_settings = db.session.query(Setting).order_by(Setting.createTime.desc()).first()
    new_room = Room(roomName=data['roomName'],
                    roomDescription=data['roomDescription'],
                    unitPrice=data['unitPrice'],
                    acTemperature=latest_settings.defaultTemperature,
                    fanSpeed=latest_settings.defaultFanSpeed,
                    acMode=latest_settings.acMode)
    db.session.add(new_room)
    db.session.commit()

    return jsonify({"msg": "创建成功"}), 201


def room_info(room: Room, require_details=False, for_manager=True):
    if room is None:
        abort(404, "room not found")
    if require_details and room.records is None:
        abort(404, "record not found")
    latest_settings = db.session.query(Setting).order_by(Setting.createTime.desc()).first()
    if require_details:
        if not for_manager:
            records = db.session.query(RoomRecord).filter_by(customSessionID=room.customerSessionID).all()
        else:
            records = db.session.query(RoomRecord).filter_by(roomID=room.roomID).all()
    else:
        records = None
    timeLeft = (datetime.now() - room.firstRuntime) / timedelta(
        minutes=2) * scheduler.boost if room.firstRuntime is not None else None
    return dict(roomID=room.roomID, roomName=room.roomName, roomDescription=room.roomDescription,
                roomTemperature=room.roomTemperature, timeLeft=timeLeft, unitPrice=room.unitPrice,
                acTemperature=max(min(room.acTemperature, latest_settings.maxTemperature),
                                  latest_settings.minTemperature),
                fanSpeed=room.fanSpeed.value, acMode=latest_settings.acMode.value,
                initialTemperature=room.initialTemperature, queueState=room.queueState.value,
                minTemperature=latest_settings.minTemperature, maxTemperature=latest_settings.maxTemperature,
                firstRunTime=room.firstRuntime, customerSessionID=room.customerSessionID, consumption=room.consumption,
                checkInTime=room.checkInTime, occupied=room.customerSessionID is not None,
                roomDetails=[record_info(record) for record in records] if records is not None else None)


def record_info(record: RoomRecord):
    return dict(id=record.id, duration=record.serveEndTime - record.serveStartTime,
                requestTime=record.requestTime, serveStartTime=record.serveStartTime, serveEndTime=record.serveEndTime,
                fanSpeed=record.fanSpeed.value, acMode=record.acMode.value, rate=record.rate,
                consumption=record.consumption, accumulatedConsumption=record.accumulatedConsumption)


def room_get(token, roomName=None):
    """
    [客户，前台，管理员]
    客户只能查看自己房间
    前台，管理员可以查看所有房间

    客户可以修改自己房间，仅限空调相关
    管理员可以修改所有房间，包括房间名和描述信息
    前台不能修改任何房间
    POST:
    # data
        # state  # 希望空调达到的状态
        # temperature
        # fanSpeed
    GET:
        # data
            # roomID, roomName, roomDescription, consumption, roomTemperature, acTemperature, fanSpeed, acMode,
            # initialTemperature, queueState, firstRunTime, customerSessionID, checkInTime, occupied
            # roomDetails
                # id, requestTime, serveStartTime, serveEndTime, fanSpeed, acMode, rate, consumption, accumulatedConsumption
    :param roomName: 房间号 (不填则根据客户信息自动导航)
    :return:
    """
    account_request = db.session.query(Account).filter_by(accountID=token).one()
    room, role_request = account_request.room, account_request.role
    if role_request != Role.manager and roomName is not None:
        abort(404, "only manager can visit other rooms")
    if role_request != Role.customer and roomName is None:
        abort(404, f"{role_request.value} need param roomName")
    room = room if role_request == Role.customer else db.session.query(Room).filter_by(roomName=roomName).one_or_none()
    if room is None:
        abort(404, f"room {roomName} not found")

    require_details = '/details/' in request.path
    roomInfo = room_info(room, require_details=require_details, for_manager=role_request == Role.manager)
    return jsonify(roomInfo=roomInfo), 200


def room_post(data, token, roomName=None):
    """
    [客户，前台，管理员]
    客户只能查看自己房间
    前台，管理员可以查看所有房间

    客户可以修改自己房间，仅限空调相关
    管理员可以修改所有房间，包括房间名和描述信息
    前台不能修改任何房间
    POST:
    # data
        # state  # 希望空调达到的状态
        # temperature
        # fanSpeed
    GET:
        # data
            # roomID, roomName, roomDescription, consumption, roomTemperature, acTemperature, fanSpeed, acMode,
            # initialTemperature, queueState, firstRunTime, customerSessionID, checkInTime, occupied
            # roomDetails
                # id, requestTime, serveStartTime, serveEndTime, fanSpeed, acMode, rate, consumption, accumulatedConsumption
    :param roomName: 房间号 (不填则根据客户信息自动导航)
    :return:
    """
    account_request = db.session.query(Account).filter_by(accountID=token).one()
    room, role_request = account_request.room, account_request.role
    if role_request != Role.manager and roomName is not None:
        abort(404, "only manager can visit other rooms")
    if role_request != Role.customer and roomName is None:
        abort(404, f"{role_request.value} need param roomName")
    room = room if role_request == Role.customer else db.session.query(Room).filter_by(roomName=roomName).one_or_none()
    if room is None:
        abort(404, f"room {roomName} not found")
    if role_request == Role.frontDesk:
        abort(403, "front-desk should not edit room states")
    latest_settings = db.session.query(Setting).order_by(Setting.createTime.desc()).first()
    if isinstance(data, dict) and len({'acTemperature', 'fanSpeed', 'state'} - set(data.keys())) > 0:  # 检测到空调状态修改请求
        if data.get('acTemperature') and latest_settings.minTemperature < int(
                data['acTemperature']) < latest_settings.maxTemperature:
            room.acTemperature = int(data['acTemperature'])
            print(room.acTemperature)
        if data.get('fanSpeed'):
            if data['fanSpeed'] in FanSpeed.__dict__.keys():
                room.fanSpeed = FanSpeed[data['fanSpeed']]
        # 检测到空调开关机请求
        if data['acState']:
            scheduler.turn_on(room)
        else:
            scheduler.turn_off(room)
    if role_request != Role.manager and (data.get('roomName') or data.get('roomDescription')):
        abort(401, "Unauthorized")
    if data.get('roomName'):  # 酒店管理员可以修改房间名和房间描述，房间的单价只有在房间创建时才能指定，不能修改
        room.roomName = data['roomName']
    if data.get('roomDescription'):
        room.roomDescription = data['roomDescription']
    db.session.commit()
    return True


def get_rooms(token):
    """
    [管理员，前台]
    查看所有房间状态
    :return:
    """
    role_request = db.session.query(Account).filter_by(accountID=token).one().role
    if role_request == Role.customer:
        abort(401, "Unauthorized")
    rooms = db.session.query(Room).all()
    rooms_info = [room_info(room) for room in rooms]
    return rooms_info


@app.route('/room/delete', methods=['POST'])
def delete_room():
    """
    [管理员]
    删除房间
    # data
        # roomName
    :return:
    """
    role_request = db.session.query(Account).filter_by(accountID=request.json['token']).one().role
    if role_request != Role.manager:
        abort(401, "Unauthorized")

    room_to_delete = db.session.query(Room).filter_by(roomName=request.json['roomName']).one_or_none()
    if room_to_delete is None:
        abort(404, "room not exists")

    if len(room_to_delete.accounts) > 0:
        abort(401, "room occupied, please check-out first")

    db.session.delete(room_to_delete)
    db.session.commit()
    return jsonify({"msg": "注销成功"}), 201



def change_settings(data):
    """
    [管理员]
    查看和修改空调设置
    # data
        # minTemperature
        # maxTemperature
        # defaultTemperature
        # acMode
        # defaultFanSpeed
        # rate
    :return:
    """
    account_request = db.session.query(Account).filter_by(username=data['token']).one()
    if account_request.role != Role.manager:
        abort(401, "Unauthorized")

    setting = Setting(rate=data['rate'], defaultFanSpeed=FanSpeed[data['defaultFanSpeed']],
                        defaultTemperature=data['defaultTemperature'], acMode=data['acMode'],
                        minTemperature=data['minTemperature'], maxTemperature=data['maxTemperature'])
    db.session.add(setting)
    db.session.commit()
   
    return True

def get_settings(name):
    account_request = db.session.query(Account).filter_by(username=name).one()
    if account_request.role != Role.manager:
        abort(401, "Unauthorized")


    setting = db.session.query(Setting).order_by(Setting.createTime.desc()).first()

    return {'settingID':setting.settingID, 'lastEditTime':setting.createTime, 'rate':setting.rate,
                   'defaultFanSpeed':setting.defaultFanSpeed.value,
                   'defaultTemperature':setting.defaultTemperature, 'minTemperature':setting.minTemperature,
                   'maxTemperature':setting.maxTemperature,
                   'acMode':setting.acMode.value}



PATH = '127.0.0.1:5000'  # '139.59.115.34:5000'


def translate(x):
    trans_dict = {
        '管理员': 'manager',
        '客户': 'customer',
        '前台': 'frontDesk',
    }
    return trans_dict[x]


class log_data():
    """
    登录注册部分可能会用到的信息
    """

    def __init__(self, username='', password='', role='', test=False):
        self.identification = '管理员'
        self.verification = True  # 是否得到内容
        self.identify = True  # 密码是否正确
        self.room_id = 101
        self.token, self.room_id = self.login1(username, password, role, test)

    def login1(self, username, password, role, test):
        test_data = {
            'username': '222',
            'password': '222',
            'role': translate('管理员')
        }
        if test:
            data = test_data
        else:
            data = {
                'username': str(username),
                'password': str(password),
                'role': translate(role)
            }
        response = login(data)
        if not response:
            self.identify = self.verification = False
            return None, None
        token = response['token']
        lst = get_rooms(token)
        room_id = None
        for dict in lst:
            if dict['customerSessionID'] == data['username']:
                room_id = dict['roomName']
        print(token, room_id)
        return token, room_id


class hotel_data():
    """
    酒店管理需要用到的信息
    """

    def __init__(self, username):
        self.room_id = None
        self.nused_id = None
        self.used_id = None
        self.username = username

    def __str__(self) -> str:
        return self.username

    def update_ac(self, room_id, input, token):
        headers = {
            'Authorization': 'Bearer ' + token
        }
        input_data = {}
        data = room_get(token=token)
        if data['queueState'] == 'IDLE':
            data['acState'] = False
        elif data['queueState'] == 'PENDING' or data['queueState'] == 'RUNNING':
            data['acState'] = True
        else:
            pass
        for name in ['acTemperature', 'fanSpeed', 'acState', 'acMode']:
            input_data[name] = data[name]
        for key, value in input.items():
            if key == 'switch':
                if value == 'true':
                    input_data['acState'] = not input_data['acState']
            else:
                input_data[key] = value
        print(input_data)
        response = room_post(data=input_data, token=headers)
        if response:
            print('更新成功')
        return data['roomTemperature']

    def room(self, token):
        """
        获取当前房屋的使用信息
        """
        data = get_rooms(token)
        self.room_id = [dict['roomName'] for dict in data]
        self.nused_id = [dict['roomName'] for dict in data if dict['occupied'] == False]
        self.used_id = [dict['roomName'] for dict in data if dict['occupied'] == True]
        return self.room_id, self.nused_id, self.used_id

    def check_in(self, user_name='', password='', idCard='', phone='', roomNumber='', token=''):
        """
        入住
        """
        print('check_in')

        data = {
            'username': user_name,
            'password': password,
            'idCard': idCard,
            'phoneNumber': phone,
            'roomName': roomNumber,
            'role': 'customer'
        }
        print(data)
        response = create_account(data, token)
        if response:
            print('success')
            return True

    def check_out(self, room_id, token):
        """
        退房
        """
        data = {
            'roomName': int(room_id)
        }
        print(data)
        response = account_delete(data, token)
        if response:
            return True, self.check_room_expense(int(room_id), token)
        else:
            return False, None

    def check(self, room_id, start_time='2023-11-21 00:00:00', end_time='2023-11-22 15:45:32'):
        '''
        查看某个房间的详单'api/logs/get_ac_info/'
        '''
        data = {
            'start_time': start_time,
            'end_time': end_time
        }
        response = requests.post('http://10.129.67.27:8000/api/logs/get_ac_info/',
                                 data=data
                                 )
        output = json.loads(response.content)['detail']
        for dict in output:
            if (dict['roomNumber'] == '房间101'):
                detial = dict
        print(detial)

        return True, data

    def check_all_log(self):
        response = requests.get('http://se.dahuangggg.me/api/logs/get_all_logs/')
        data = json.loads(response.content)['log']
        return data

    def check_room_expense(self, room_id, token):
        # response = requests.get(f'http://{PATH}/room-details/{int(room_id)}', headers=headers)
        # if response.status_code != 200:
        #     return []
        # data = json.loads(response.content)['roomDetails']
        return {'data':[1,2]}

    def getoperate(self,name):
        """
        查看系统设置
        """
        temp_upper_limit = 10
        temp_lower_limit = 1
        result = get_settings(name)
        temp_upper_limit = result['maxTemperature']
        temp_lower_limit = result['minTemperature']
        work_mode = result['acMode']
        rate = result['rate']
        speed_rates = {'low': rate, 'medium': rate, 'high': rate}
        print('get_mode:',work_mode)
        return temp_upper_limit, temp_lower_limit, work_mode, speed_rates

    def operate_set(self, token, temp_upper_limit, temp_lower_limit, work_mode, rate_low, rate_medium, rate_high):
        """
        依据传输的内容修改系统设置
        """

        data = {
            'token':token,
            'rate':rate_low,
            'defaultFanSpeed':'MEDIUM',
            'defaultTemperature':24,
            'acMode':work_mode,
            'maxTemperature':temp_upper_limit,
            'minTemperature':temp_lower_limit,
        }
        if change_settings(data=data):
            print('更改成功')
            return True
        else:
            return False

    def query_all_room(self, token):
        res = get_rooms(token)
        return res


log_and_submit = Blueprint('log_and_submit', __name__)


@log_and_submit.route('/')
def log_and_submit_login():
    """
    进入网页后先登录
    :return: 返回一个本地的网页内容
    """
    if 'username' in session:
        if session['username'] == '客户':
            return redirect(url_for('customer.homepage'))  # 导入到对应的首页
        elif session['username'] == '管理员' or session['username'] == '前台':
            return redirect(url_for('hotel_receptionist.homepage'))  # 导入到对应的首页
    else:
        return render_template('login.html')


@log_and_submit.route('/submit', methods=['POST', 'GET'])
def submit():
    """
    在登陆提交表单后依据表单中的内容确定要转到哪边，并且依据身份建立对应对话session['username']=?，如果是某个房间的使用者session可以加上对应的房间号
    return:
    """
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        roll = request.form['roll']
    else:
        username = request.args.get('username')
        password = request.args.get('password')
        roll = request.args.get('roll')

    try:
        # 申请数据库对应的内容，返回字典与是否正确，不正确则弹出错误转到except部分
        dic = log_data(username, password, roll)
        print(dic.verification)
        if dic.verification == True and dic.identify == True:
            # 如果正确，则依据身份不同建立对应的session，包括房间号等
            session['username'] = username
            session['identification'] = roll
            session['token'] = dic.token
            if session['identification'] == '客户':
                session['room_id'] = dic.room_id
                return redirect(url_for('customer.homepage'))
            else:
                return redirect(url_for('hotel_receptionist.homepage'))
        raise Exception("Verification failed")  # 只要工作做不成就报错转到except，成了直接返回走

    except:
        #   出现查询不到对应内容，账号密码错误的时候弹到让这部分
        return '账号密码不正确或网络错误'


hotel_receptionist = Blueprint('hotel_receptionist', __name__)


# 功能：办理入住，打印某房间详单，退房，查询或修改某房间状态
@hotel_receptionist.route('/')
def homepage():
    '''
    检查是否是前台，返回前台的首页
    :return: 首页
    '''
    if 'username' in session:
        # 检查是不是前台，是的话返回前台首页，不是的话返回顾客首页
        if session['identification'] == '客户':
            return redirect(url_for('customer.homepage'))
        else:
            return render_template('receptionist_homepage.html', name=session['username'])
        pass

    else:
        # 连注册都没注册的话送到登录页面去
        return redirect(url_for('log_and_submit.log_and_submit_login'))


@hotel_receptionist.route('/query')
def query():
    """
    查询房间状态，返回一个列表
    :return: 列表展示的页面，包括返回的相关内容
    """
    if 'username' in session:
        # 检查是不是前台，是的话返回前台首页，不是的话返回顾客首页
        if session['identification'] == '客户':
            return redirect(url_for('customer.homepage'))
        else:
            action = request.args.get('action')
            dic = hotel_data(session['username'])
            dic.room(session['token'])
            print(dic.room_id, dic.used_id, dic.used_id)
            if action == 'check_in':
                return render_template('query.html', list1=dic.room_id, list2=dic.nused_id, message='该房间已被使用',
                                       target_url='/receptionist/check_in')
            elif action == 'print_receipt':
                return render_template('query.html', list1=dic.room_id, list2=dic.used_id, message='该房间无人使用',
                                       target_url='/receptionist/print_receipt')
            elif action == 'check_out':
                return render_template('query.html', list1=dic.room_id, list2=dic.used_id, message='该房间无人使用',
                                       target_url='/receptionist/check_out')
            elif action == 'look':
                return render_template('query.html', list1=dic.room_id, list2=dic.room_id, message='啊？',
                                       target_url='/receptionist/look')
        pass

    else:
        # 连注册都没注册的话送到登录页面去
        return redirect(url_for('log_and_submit.log_and_submit_login'))


@hotel_receptionist.route('/check_in', methods=['POST', 'GET'])
def check_in():
    """
    办理入住，输入相关信息，办理入住，修改状态
    :return: 办理成功或失败的信息
    """
    if 'username' in session:
        # 检查是不是前台，是的话返回前台首页，不是的话返回顾客首页
        if session['identification'] == '客户':
            return redirect(url_for('customer.homepage'))
        else:
            if request.method == 'POST':
                password = request.form['password']
                room_id = request.form['roomNumber']
                user_name = request.form['user_name']
                dic = hotel_data('username')
                print(password, room_id, user_name)
                try:
                    if dic.check_in(roomNumber=room_id, password=password, token=session['token'], user_name=user_name):
                        return render_template('good_check_in.html', roomNumber=room_id)
                    raise Exception("Verification failed")  # 只要工作做不成就报错转到except，成了直接返回走
                except:
                    return '房间号不正确或网络错误'
            else:
                room_id = request.args.get('element')
                return render_template('check-in.html', roomNumber=room_id)
        pass

    else:
        # 连注册都没注册的话送到登录页面去
        return redirect(url_for('log_and_submit.log_and_submit_login'))


@hotel_receptionist.route('/check_out')
def check_out():
    """
    办理退房，将状态修改回最初
    :return: 办理成功或失败的信息
    """
    if 'username' in session:
        # 检查是不是前台，是的话返回前台首页，不是的话返回顾客首页
        if session['identification'] == '客户':
            return redirect(url_for('customer.homepage'))
        else:
            dic = hotel_data(session['username'])
            room_id = request.args.get('element')
            judgment, data = dic.check_out(room_id, session['token'])
            if judgment:
                # 创建Excel文件
                df = pd.DataFrame(data)
                filename = f'checkout_{room_id}.xlsx'
                df.to_excel(filename, index=False)
                # 在session中存储文件名
                session['excel_filename'] = filename
                return render_template('good_check_out.html', room_id=room_id)
            else:
                return '房间号不正确或网络错误'
        pass

    else:
        # 连注册都没注册的话送到登录页面去
        return redirect(url_for('log_and_submit.log_and_submit_login'))


@hotel_receptionist.route('/download_excel')
def download_excel():
    filename = session['excel_filename']
    if filename and os.path.exists(filename):
        response = send_file(filename, as_attachment=True)
        return response
    else:
        return "文件不存在", 404


@hotel_receptionist.route('/query_all')
def query_all():
    """
    查询全部信息
    :return :信息页面
    """
    if 'username' in session:
        # 检查是不是前台，是的话返回前台首页，不是的话返回顾客首页
        if session['identification'] == '客户':
            return redirect(url_for('customer.homepage'))
        else:
            return render_template('receptionist_homepage.html', name=session['username'])
        pass

    else:
        # 连注册都没注册的话送到登录页面去
        return redirect(url_for('log_and_submit.log_and_submit_login'))


@hotel_receptionist.route('/print_receipt')
def print_receipt():
    """
    打印收据
    :return :下载excel表格
    """
    if 'username' in session:
        # 检查是不是前台，是的话返回前台首页，不是的话返回顾客首页
        if session['identification'] == '客户':
            return redirect(url_for('customer.homepage'))
        else:
            dic = hotel_data(session['username'])
            room_id = request.args.get('element')
            data = dic.check_room_expense(room_id, session['token'])
            if data:
                df = pd.DataFrame(data)
                filename = f'checkout_{room_id}.xlsx'
                df.to_excel(filename, index=False)
                # 在session中存储文件名
                session['excel_filename'] = filename
                return render_template('print_receipt.html', room_id=room_id)
            else:
                return '房间号不正确或网络错误'
    else:
        # 连注册都没注册的话送到登录页面去
        return redirect(url_for('log_and_submit.log_and_submit_login'))


@hotel_receptionist.route('/change')
def change():
    """
    依据方法进入对应房间修改页面或者修改房间内容
    :return :下载excel表格
    """
    if 'username' in session:
        # 检查是不是前台，是的话返回前台首页，不是的话返回顾客首页
        if session['identification'] == '客户':
            return redirect(url_for('customer.homepage'))
        else:
            return render_template('receptionist_homepage.html', name=session['username'])
    else:
        # 连注册都没注册的话送到登录页面去
        return redirect(url_for('log_and_submit.log_and_submit_login'))


@hotel_receptionist.route('/operate_set', methods=['POST', 'GET'])
def operate_set():
    """
    依据方法进入对应房间修改页面或者修改房间内容
    :return :下载excel表格
    """
    if 'username' in session:
        # 检查是不是前台，是的话返回前台首页，不是的话返回顾客首页
        if session['identification'] == '客户':
            return redirect(url_for('customer.homepage'))
        else:
            dic = hotel_data(session['username'])
            if request.method == 'GET':
                try:
                    temp_upper_limit, temp_lower_limit, work_modes, speed_rates = dic.getoperate(session['username'])
                    return render_template('operate_set.html',
                                           temp_upper_limit=temp_upper_limit,
                                           temp_lower_limit=temp_lower_limit,
                                           default_mode=work_modes,
                                           speed_rates=speed_rates
                                           )
                except:
                    return '网络/权限出现问题'
            else:
                try:
                    dic = hotel_data(session['username'])
                    temp_upper_limit = request.form.get('tempUpperLimit')
                    temp_lower_limit = request.form.get('tempLowerLimit')
                    work_mode = request.form.get('workMode')
                    rate_low = request.form.get('rateLow')
                    rate_medium = request.form.get('rateMedium')
                    rate_high = request.form.get('rateHigh')
                    print(temp_upper_limit, temp_lower_limit, work_mode, rate_low, rate_medium, rate_high)
                    if not dic.operate_set(session['username'],temp_upper_limit, temp_lower_limit, work_mode, rate_low, rate_medium,
                                           rate_high):
                        raise Exception("Verification failed")
                    return render_template('receptionist_homepage.html', name=session['username'])
                except:
                    return '网络/权限出现问题'
    else:
        # 连注册都没注册的话送到登录页面去
        return redirect(url_for('log_and_submit.log_and_submit_login'))


@hotel_receptionist.route('/log_out')
def log_out():
    """
    依据方法进入对应房间修改页面或者修改房间内容
    :return :下载excel表格
    """
    if 'username' in session:
        # 检查是不是前台，是的话返回前台首页，不是的话返回顾客首页
        if session['identification'] == '客户':
            return redirect(url_for('customer.homepage'))
        else:
            session.clear()
            return redirect(url_for('log_and_submit.log_and_submit_login'))
    else:
        # 连注册都没注册的话送到登录页面去
        return redirect(url_for('log_and_submit.log_and_submit_login'))


@hotel_receptionist.route('/query_all_room/')
def query_all_room_detail():
    """
    查询所有房间的信息
    :return :下载excel表格
    """
    if 'username' in session:
        # 检查是不是前台，是的话返回前台首页，不是的话返回顾客首页
        if session['identification'] == '客户':
            return redirect(url_for('customer.homepage'))
        else:
            dic = hotel_data(session['username'])
            rooms = dic.query_all_room()
            return render_template('query_all_rooms.html', rooms=rooms)

    else:
        # 连注册都没注册的话送到登录页面去
        return redirect(url_for('log_and_submit.log_and_submit_login'))


customer = Blueprint('customer', __name__)

base_data = {
    'roomNumber': 0,
    'currentTemperature': 0,
    'targetTemperature': 0,
    'acStatus': '',
    'acMode': '',
    'cost': 0,
    'totalCost': 0,
    'queueStatus': '',
}


@customer.route('/')
def homepage():
    """
    检查是否是房间使用者，返回使用者房间的首页
    :return: 首页
    """
    if 'username' in session:
        if session['identification'] == '客户':
            function = hotel_data('')
            session['room_temp'] = function.update_ac(session['room_id'], request.form.to_dict(), session['token'])
            return render_template('customer_homepage.html', room_temp=session['room_temp'])
        else:
            return render_template('customer_homepage.html')


    else:
        # 连注册都没注册的话送到登录页面去
        # return render_template('customer_homepage.html')
        return redirect(url_for('log_and_submit.log_and_submit_login'))


@customer.route('/open_condition')
def open_condition():
    """
    依据数据库内容开启或关闭空调（修改对应空调状态）
    :return: 成功开启/关闭空调的信息或者未能成功关闭
    """
    if 'username' in session:
        # 检查是不是房间使用者，是的话依据数据库内容开启或关闭空调（修改对应空调状态），不是的话返回管理者或者前台首页
        # if session['username' ]==?:
        #     return render_template('')
        # else:
        #     url_for('customer.homepage')
        pass

    else:
        # 连注册都没注册的话送到登录页面去
        return redirect(url_for('log_and_submit.log_and_submit_login'))


@customer.route('/air_conditioner/', methods=['POST'])
def post():
    print('start')
    print(request.form.to_dict())
    print(session)
    if 'username' in session:
        print('username')
        if session['identification'] == '客户':
            if 'room_id' in session:
                function = hotel_data('')
                print(session)
                session['room_temp'] = function.update_ac(session['room_id'], request.form.to_dict(), session['token'])
                return jsonify({'msg': '成功'}), 200
            else:
                return jsonify({'msg': '请先登记入住'}), 404


@customer.route('/check')
def check():
    """
    检查自身房间的状态
    :return: 房间状态信息页面
    """
    if 'username' in session:
        # 检查是不是房间使用者，是的话查询数据库并返回空调状态页面，不是的话返回管理者或者顾客首页
        # if session['username' ]==?:
        #     return render_template('')
        # else:
        #     url_for('customer.homepage')
        pass

    else:
        # 连注册都没注册的话送到登录页面去
        return redirect(url_for('log_and_submit.log_and_submit_login'))


@customer.route('/change')
def change():
    """
    修改自身房间的状态
    :return: 修改是否成功的信息
    """
    if 'username' in session:
        # 检查是不是房间使用者，是的话依据对应信息进行修改，返回是否成功，不是的话返回管理者或者顾客首页
        # if session['username' ]==?:
        #     return render_template('')
        # else:
        #     url_for('customer.homepage')
        pass

    else:
        # 连注册都没注册的话送到登录页面去
        return redirect(url_for('log_and_submit.log_and_submit_login'))


# 注册蓝图
app.register_blueprint(log_and_submit, url_prefix='/')
app.register_blueprint(customer, url_prefix='/customer')
app.register_blueprint(hotel_receptionist, url_prefix='/receptionist')
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0',port=3000)
