import React, { useState } from 'react';
import { Job } from '../types';
import { Play, Pause, RotateCcw, Trash2, ChevronDown, ChevronUp } from 'lucide-react';
import { jobsApi } from '../services/api';

interface JobCardProps {
  job: Job;
  onRequeue: (jobId: string) => void;
  onResume: (jobId: string) => void;
  onSuspend: (jobId: string) => void;
  onDelete: (jobId: string) => void;
}

const getStatusIcon = (stat: number) => {
  switch (stat) {
    case 3: return '✅'; // Completed
    case 2: return '⏸️'; // Suspended
    case 1: return '▶️'; // Active
    case 4: return '❌'; // Failed
    case 6: return '⏳'; // Pending
    default: return '❓'; // Unknown
  }
};

const getStatusText = (stat: number) => {
  switch (stat) {
    case 3: return 'Completed';
    case 2: return 'Suspended';
    case 1: return 'Active';
    case 4: return 'Failed';
    case 6: return 'Pending';
    default: return 'Unknown';
  }
};

const getStatusColor = (stat: number) => {
  switch (stat) {
    case 3: return 'text-tg-accent'; // Completed
    case 2: return 'text-tg-hint'; // Suspended
    case 1: return 'text-tg-button'; // Active
    case 4: return 'text-tg-destructive'; // Failed
    case 6: return 'text-tg-link'; // Pending
    default: return 'text-tg-hint'; // Unknown
  }
};

// Определяем приоритет статусов как в боте
const getBatchStatus = (statusList: number[]): number => {
  if (statusList.includes(1)) return 1;      // Active
  if (statusList.includes(6)) return 6;      // Pending
  if (statusList.includes(2)) return 2;      // Suspended
  if (statusList.includes(4)) return 4;      // Failed
  if (statusList.every(s => s === 3)) return 3;  // Completed
  return 0;                                  // Unknown
};

export const JobCard: React.FC<JobCardProps> = ({
  job,
  onRequeue,
  onResume,
  onSuspend,
  onDelete,
}) => {
  const [isExpanded, setIsExpanded] = useState(false);
  const [batchJobs, setBatchJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(false);

  const progress = job.Props.Tasks > 0 
    ? Math.round((job.CompletedChunks / job.Props.Tasks) * 100)
    : 0;

  const handleCardClick = async () => {
    if (!isExpanded && batchJobs.length === 0) {
      setLoading(true);
      try {
        const jobs = await jobsApi.getJobsByBatch(job.Props.Batch);
        // Сохраняем все задачи из batch
        setBatchJobs(jobs);
      } catch (error) {
        console.error('Error loading batch jobs:', error);
      } finally {
        setLoading(false);
      }
    }
    setIsExpanded(!isExpanded);
  };

  // Проверяем, есть ли другие задачи в этом batch
  const hasBatchJobs = batchJobs.length > 1;

  return (
    <div className="bg-tg-section rounded-lg shadow-sm border border-tg-secondary-bg overflow-hidden">
      <div 
        className={`p-4 space-y-3 ${job.batchSize && job.batchSize > 1 ? 'cursor-pointer hover:bg-tg-secondary-bg transition-colors' : ''}`}
        onClick={job.batchSize && job.batchSize > 1 ? handleCardClick : undefined}
      >
        <div className="flex items-start justify-between">
          <div className="flex-1 min-w-0">
            <div className="flex items-center">
              <h3 className="text-sm font-medium text-tg-base truncate mr-2">
                {job.Props.Batch}
              </h3>
              {job.batchSize && job.batchSize > 1 && (
                isExpanded ? (
                  <ChevronUp className="w-4 h-4 text-tg-hint" />
                ) : (
                  <ChevronDown className="w-4 h-4 text-tg-hint" />
                )
              )}
            </div>
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

        <div className="flex space-x-2 pt-2" onClick={e => e.stopPropagation()}>
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

      {/* Expanded content */}
      <div 
        className={`overflow-hidden transition-all duration-300 ${
          isExpanded ? 'max-h-[1000px]' : 'max-h-0'
        }`}
      >
        {loading ? (
          <div className="flex justify-center py-4">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-tg-button"></div>
          </div>
        ) : (
          <div className="divide-y divide-tg-secondary-bg">
            {batchJobs
              .filter(batchJob => batchJob._id !== job._id)  // Фильтруем здесь, чтобы правильно определять hasBatchJobs
              .map(batchJob => (
                <div key={batchJob._id} className="p-4 space-y-3 bg-tg-secondary-bg/50">
                  <div className="flex items-start justify-between">
                    <div className="flex-1 min-w-0">
                      <h3 className="text-sm font-medium text-tg-base truncate">
                        {batchJob.Props.Name}
                      </h3>
                    </div>
                    <div className="flex items-center space-x-1 ml-2">
                      <span className="text-lg">{getStatusIcon(batchJob.Stat)}</span>
                      <span className={`text-xs font-medium ${getStatusColor(batchJob.Stat)}`}>
                        {getStatusText(batchJob.Stat)}
                      </span>
                    </div>
                  </div>

                  <div className="space-y-2">
                    <div className="flex justify-between text-xs text-tg-subtitle">
                      <span>Progress</span>
                      <span>
                        {batchJob.Props.Tasks > 0 
                          ? Math.round((batchJob.CompletedChunks / batchJob.Props.Tasks) * 100)
                          : 0}% 
                        ({batchJob.CompletedChunks}/{batchJob.Props.Tasks})
                      </span>
                    </div>
                    <div className="w-full bg-tg-secondary-bg rounded-full h-2">
                      <div
                        className="bg-tg-button h-2 rounded-full transition-all duration-300"
                        style={{ 
                          width: `${batchJob.Props.Tasks > 0 
                            ? Math.round((batchJob.CompletedChunks / batchJob.Props.Tasks) * 100)
                            : 0}%` 
                        }}
                      />
                    </div>
                  </div>

                  <div className="flex space-x-2 pt-2">
                    {batchJob.Stat === 2 && (
                      <button
                        onClick={() => onResume(batchJob._id)}
                        className="flex-1 flex items-center justify-center px-3 py-2 text-xs font-medium text-tg-accent bg-tg-secondary-bg rounded-md hover:opacity-90 transition-opacity"
                      >
                        <Play className="w-4 h-4 mr-1" />
                        Resume
                      </button>
                    )}
                    
                    {batchJob.Stat === 1 && (
                      <button
                        onClick={() => onSuspend(batchJob._id)}
                        className="flex-1 flex items-center justify-center px-3 py-2 text-xs font-medium text-tg-hint bg-tg-secondary-bg rounded-md hover:opacity-90 transition-opacity"
                      >
                        <Pause className="w-4 h-4 mr-1" />
                        Suspend
                      </button>
                    )}
                    
                    <button
                      onClick={() => onRequeue(batchJob._id)}
                      className="flex-1 flex items-center justify-center px-3 py-2 text-xs font-medium bg-tg-button text-tg-button-text rounded-md hover:opacity-90 transition-opacity"
                    >
                      <RotateCcw className="w-4 h-4 mr-1" />
                      Requeue
                    </button>
                    
                    <button
                      onClick={() => onDelete(batchJob._id)}
                      className="flex-1 flex items-center justify-center px-3 py-2 text-xs font-medium text-tg-destructive bg-tg-secondary-bg rounded-md hover:opacity-90 transition-opacity"
                    >
                      <Trash2 className="w-4 h-4 mr-1" />
                      Delete
                    </button>
                  </div>
                </div>
              ))}
          </div>
        )}
      </div>
    </div>
  );
}; 