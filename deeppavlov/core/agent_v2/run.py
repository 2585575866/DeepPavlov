from datetime import datetime
from typing import Collection, Optional, List

from telebot.types import Message, Location, User

from utils.telegram_utils.telegram_ui import experimental_bot


def model_function():
    from deeppavlov import configs

    from deeppavlov.core.agent_v2.agent import Agent
    from deeppavlov.core.agent_v2.state_manager import StateManager
    from deeppavlov.core.agent_v2.preprocessor import RestPreprocessor
    from deeppavlov.core.agent_v2.skill_manager import SkillManager
    from deeppavlov.core.agent_v2.rest_caller import RestCaller
    from deeppavlov.core.agent_v2.response_selector import ConfidenceResponseSelector
    from deeppavlov.core.agent_v2.config import MAX_WORKERS, ANNOTATORS
    # from deeppavlov.core.agent_v2.bot import BOT

    import logging

    logging.getLogger('requests.packages.urllib3.connectionpool').setLevel(logging.WARNING)

    state_manager = StateManager()

    names, urls = zip(*[(annotator['name'], annotator['url']) for annotator in ANNOTATORS])
    preprocessor = RestPreprocessor(names, urls)

    rest_caller = RestCaller(max_workers=MAX_WORKERS)
    response_selector = ConfidenceResponseSelector()
    skill_manager = SkillManager(skills_selector=None, response_selector=response_selector, rest_caller=rest_caller)

    agent = Agent(state_manager, preprocessor, skill_manager)

    def infer(messages: Collection[Message], dialog_ids):
        utterances: List[Optional[str]] = [message.text for message in messages]
        tg_users: List[User] = [message.from_user for message in messages]

        u_tg_ids = [str(user.id) for user in tg_users]
        u_tg_data = [{
            'id': user.id,
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name
        }
                     for user in tg_users]

        u_d_types = [None] * len(messages)
        date_times = [datetime.utcnow()] * len(messages)
        locations: List[Optional[Location]] = [message.location for message in messages]
        ch_types = ['telegram'] * len(messages)

        answers = agent(utterances=utterances, user_telegram_ids=u_tg_ids, user_device_types=u_d_types,
                        date_times=date_times, locations=locations, channel_types=ch_types)
        return answers

    return infer


experimental_bot(model_function, token='',
                 proxy='')
