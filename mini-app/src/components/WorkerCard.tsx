import React from 'react';
import { Worker } from '../types';

interface WorkerCardProps {
  worker: Worker;
}

const getStatusIcon = (stat: number) => {
  switch (stat) {
    case 0: return '❓'; // Unknown
    case 1: return '🔄'; // Rendering
    case 2: return '💤'; // Idle
    case 3: return '🔴'; // Offline
    case 4: return '⚠️'; // Stalled
    case 8: return '🚀'; // StartingJob
    default: return '❓';
  }
};

const getStatusText = (stat: number) => {
  switch (stat) {
    case 0: return 'Unknown';
    case 1: return 'Rendering';
    case 2: return 'Idle';
    case 3: return 'Offline';
    case 4: return 'Stalled';
    case 8: return 'StartingJob';
    default: return 'Unknown';
  }
};

const getStatusColor = (stat: number) => {
  switch (stat) {
    case 0: return 'text-tg-hint'; // Unknown
    case 1: return 'text-tg-accent'; // Rendering
    case 2: return 'text-tg-link'; // Idle
    case 3: return 'text-tg-destructive'; // Offline
    case 4: return 'text-tg-destructive'; // Stalled
    case 8: return 'text-tg-accent'; // StartingJob
    default: return 'text-tg-hint';
  }
};

export const WorkerCard: React.FC<WorkerCardProps> = ({ worker }) => {
  return (
    <div className="bg-tg-section rounded-lg shadow-sm border border-tg-secondary-bg p-3">
      <div className="flex items-center justify-between">
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-medium text-tg-base truncate">
            {worker.Info.Name}
          </h3>
        </div>
        <div className="flex items-center space-x-2 ml-2">
          <span className="text-sm">{getStatusIcon(worker.Info.Stat)}</span>
          <span className={`text-xs ${getStatusColor(worker.Info.Stat)}`}>
            {getStatusText(worker.Info.Stat)}
          </span>
        </div>
      </div>
    </div>
  );
}; 