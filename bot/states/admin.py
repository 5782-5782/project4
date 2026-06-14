from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    waiting_rules = State()
    waiting_subadmin_id = State()
    waiting_subadmin_limit = State()
    waiting_subadmin_name = State()
    editing_subadmin_limit = State()
