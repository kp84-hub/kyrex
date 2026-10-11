import React from 'react';
import DeveloperRepository from './DeveloperRepository.jsx';

export default function ChatRepository({ bots = [], activeBotId, onChanged, taskRunning }) {
  const bot = bots.find((item) => item.id === activeBotId);
  const available = bot && (bot.repository_available === true ||
    (bot.repository_available === undefined && bot.role?.id === 'developer'));
  if (!available) return null;
  return <DeveloperRepository key={bot.id} bot={bot} onChanged={onChanged}
    compact taskRunning={taskRunning} />;
}
