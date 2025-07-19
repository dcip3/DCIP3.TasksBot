import React, { useState } from 'react';
import { Job } from '../types';
import { Play, Pause, RotateCcw, Trash2 } from 'lucide-react';

interface JobCardProps {
  job: Job;
  onRequeue: (jobId: string) => void;
  onResume: (jobId: string) => void;
  onSuspend: (jobId: string) => void;
  onDelete: (jobId: string) => void;
}

const getStatusIcon = (stat: number) => {
  switch (stat) {
    case 0: return '⏳'; // Unknown
    case 1: return '🔄'; // Active
    case 2: return '⏸️'; // Suspended
    case 3: return '✅'; // Completed
    case 4: return '❌'; // Failed
    case 6: return '⏳'; // Pending
    default: return '❓';
  }
};

const getStatusColor = (stat: number) => {
  switch (stat) {
    case 1: return 'text-tg-accent'; // Active
    case 2: return 'text-tg-hint'; // Suspended
    case 3: return 'text-tg-link'; // Completed
    case 4: return 'text-tg-destructive'; // Failed
    case 6: return 'text-tg-hint'; // Pending
    default: return 'text-tg-hint';
  }
};

const getStatusText = (stat: number) => {
  switch (stat) {
    case 0: return 'Unknown';
    case 1: return 'Active';
    case 2: return 'Suspended';
    case 3: return 'Completed';
    case 4: return 'Failed';
    case 6: return 'Pending';
    default: return 'Unknown';
  }
};

export const JobCard: React.FC<JobCardProps> = ({
  job,
  onRequeue,
  onResume,
  onSuspend,
  onDelete,
}) => {
  const progress = job.Props.Tasks > 0 
    ? Math.round((job.CompletedChunks / job.Props.Tasks) * 100)
    : 0;

  return (
    <div className="bg-tg-section rounded-lg shadow-sm border border-tg-secondary-bg p-4 space-y-3">
      <div className="flex items-start justify-between">
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-medium text-tg-base truncate">
            {job.Props.Name}
          </h3>
        </div>
        <div className="flex items-center space-x-1 ml-2">
          <span className="text-lg">{getStatusIcon(job.Stat)}</span>
          <span className={`text-xs font-medium ${getStatusColor(job.Stat)}`}>
            {getStatusText(job.Stat)}
          </span>
        </div>
      </div>

      <div className="space-y-2">
        <div className="flex justify-between text-xs text-tg-subtitle">
          <span>Progress</span>
          <span>{progress}% ({job.CompletedChunks}/{job.Props.Tasks})</span>
        </div>
        <div className="w-full bg-tg-secondary-bg rounded-full h-2">
          <div
            className="bg-tg-button h-2 rounded-full transition-all duration-300"
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>

      <div className="flex space-x-2 pt-2">
        {job.Stat === 2 && (
          <button
            onClick={() => onResume(job._id)}
            className="flex-1 flex items-center justify-center px-3 py-2 text-xs font-medium text-tg-accent bg-tg-secondary-bg rounded-md hover:opacity-90 transition-opacity"
          >
            <Play className="w-4 h-4 mr-1" />
            Resume
          </button>
        )}
        
        {job.Stat === 1 && (
          <button
            onClick={() => onSuspend(job._id)}
            className="flex-1 flex items-center justify-center px-3 py-2 text-xs font-medium text-tg-hint bg-tg-secondary-bg rounded-md hover:opacity-90 transition-opacity"
          >
            <Pause className="w-4 h-4 mr-1" />
            Suspend
          </button>
        )}
        
        <button
          onClick={() => onRequeue(job._id)}
          className="flex-1 flex items-center justify-center px-3 py-2 text-xs font-medium bg-tg-button text-tg-button-text rounded-md hover:opacity-90 transition-opacity"
        >
          <RotateCcw className="w-4 h-4 mr-1" />
          Requeue
        </button>
        
        <button
          onClick={() => onDelete(job._id)}
          className="flex-1 flex items-center justify-center px-3 py-2 text-xs font-medium text-tg-destructive bg-tg-secondary-bg rounded-md hover:opacity-90 transition-opacity"
        >
          <Trash2 className="w-4 h-4 mr-1" />
          Delete
        </button>
      </div>
    </div>
  );
}; 