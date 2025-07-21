import React from 'react';
import { Job, Task } from '../types';
import { X, Play, Pause, RotateCcw, Trash2, Eye } from 'lucide-react';

interface JobDetailsModalProps {
  job: Job | null;
  tasks: Task[];
  isOpen: boolean;
  onClose: () => void;
  onRequeue: (jobId: string) => void;
  onResume: (jobId: string) => void;
  onSuspend: (jobId: string) => void;
  onDelete: (jobId: string) => void;
  loading?: boolean;
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

const getStatusText = (stat: number) => {
  switch (stat) {
    case 0: return 'Неизвестно';
    case 1: return 'Активен';
    case 2: return 'Приостановлен';
    case 3: return 'Завершен';
    case 4: return 'Ошибка';
    case 6: return 'Ожидает';
    default: return 'Неизвестно';
  }
};

const getStatusColor = (stat: number) => {
  switch (stat) {
    case 1: return 'text-green-600'; // Active
    case 2: return 'text-yellow-600'; // Suspended
    case 3: return 'text-telegram-primary'; // Completed
    case 4: return 'text-red-600'; // Failed
    case 6: return 'text-telegram-gray'; // Pending
    default: return 'text-telegram-gray';
  }
};

const getTaskIcon = (stat: number): string => {
  switch (stat) {
    case 5: return "✅"; // Completed
    case 4: return "▶️"; // Rendering
    case 3: return "⏸️"; // Suspended
    case 6: return "❌"; // Failed
    case 2:
    case 8: return "⏳"; // Queued or Pending
    default: return "❓"; // Unknown
  }
};

const formatDuration = (startTime: string, completionTime?: string): string => {
  if (!startTime || startTime === "0001-01-01T00:00:00Z") return "";
  
  try {
    const start = new Date(startTime);
    const end = completionTime && completionTime !== "0001-01-01T00:00:00Z" 
      ? new Date(completionTime)
      : new Date();
    
    const duration = end.getTime() - start.getTime();
    const hours = Math.floor(duration / (1000 * 60 * 60));
    const minutes = Math.floor((duration % (1000 * 60 * 60)) / (1000 * 60));
    const seconds = Math.floor((duration % (1000 * 60)) / 1000);
    
    return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
  } catch {
    return "";
  }
};

const formatTaskTime = (task: Task): string => {
  const startStr = task.StartRen;
  if (!startStr || startStr === "0001-01-01T00:00:00Z") return "";

  try {
    const startTime = new Date(startStr);
    // Completed tasks
    if (task.Stat === 5) {
      const compStr = task.Comp;
      if (compStr && compStr !== "0001-01-01T00:00:00Z") {
        const compTime = new Date(compStr);
        return formatDuration(startStr, compStr);
      }
    }
    // Currently rendering tasks
    else if (task.Stat === 4) {
      return formatDuration(startStr);
    }
  } catch {
    return "";
  }
  return "";
};

export const JobDetailsModal: React.FC<JobDetailsModalProps> = ({
  job,
  tasks,
  isOpen,
  onClose,
  onRequeue,
  onResume,
  onSuspend,
  onDelete,
  loading = false,
}) => {
  if (!isOpen || !job) return null;

  const progress = job.Props.Tasks > 0 
    ? Math.round((job.CompletedChunks / job.Props.Tasks) * 100)
    : 0;

  return (
    <div className="fixed inset-0 flex items-center justify-center z-50 p-4">
      <div className="absolute inset-0 bg-tg-bg opacity-95" onClick={onClose} />
      <div className="bg-tg-bg rounded-lg max-w-4xl w-full max-h-[90vh] overflow-y-auto relative border border-tg-secondary-bg shadow-lg">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-tg-secondary-bg bg-tg-secondary-bg/30">
          <div>
            <h2 className="text-xl font-semibold text-tg-text">{job.Props.Name.split('/').pop()}</h2>
            <p className="text-sm text-tg-hint mt-1">Batch: {job.Props.Batch}</p>
          </div>
          <button
            onClick={onClose}
            className="text-tg-hint hover:text-tg-text transition-colors"
          >
            <X className="w-6 h-6" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-6 bg-tg-secondary-bg/10">
          {/* Progress */}
          <div className="space-y-3">
            <div className="flex justify-between text-sm">
              <span className="text-tg-hint">Progress:</span>
              <span className="font-medium text-tg-text">{job.CompletedChunks} / {job.Props.Tasks}</span>
            </div>
            <div className="w-full bg-tg-secondary-bg rounded-full h-3">
              <div
                className="bg-tg-button h-3 rounded-full transition-all duration-300"
                style={{ width: `${progress}%` }}
              />
            </div>
            <div className="text-center text-sm font-medium text-tg-text">
              {progress}%
            </div>
          </div>

          {/* Actions */}
          <div className="grid grid-cols-2 gap-2">
            <div className="col-span-2 flex space-x-2">
              {job.Stat === 2 && (
                <button
                  onClick={() => onResume(job._id)}
                  className="flex-1 flex items-center justify-center px-3 py-2 text-sm font-medium text-tg-accent bg-tg-secondary-bg rounded-md hover:opacity-90 transition-opacity"
                >
                  <Play className="w-4 h-4 mr-1" />
                  Resume
                </button>
              )}
              
              {job.Stat === 1 && (
                <button
                  onClick={() => onSuspend(job._id)}
                  className="flex-1 flex items-center justify-center px-3 py-2 text-sm font-medium text-tg-hint bg-tg-secondary-bg rounded-md hover:opacity-90 transition-opacity"
                >
                  <Pause className="w-4 h-4 mr-1" />
                  Suspend
                </button>
              )}
              
              <button
                onClick={() => onRequeue(job._id)}
                className="flex-1 flex items-center justify-center px-3 py-2 text-sm font-medium bg-tg-button text-tg-button-text rounded-md hover:opacity-90 transition-opacity"
              >
                <RotateCcw className="w-4 h-4 mr-1" />
                Requeue
              </button>
            </div>
            
            <button
              onClick={() => {/* TODO: Implement preview functionality */}}
              className="flex-1 flex items-center justify-center px-3 py-2 text-sm font-medium text-tg-accent bg-tg-secondary-bg rounded-md hover:opacity-90 transition-opacity"
            >
              <Eye className="w-4 h-4 mr-1" />
              Preview
            </button>

            <button
              onClick={() => onDelete(job._id)}
              className="flex-1 flex items-center justify-center px-3 py-2 text-sm font-medium text-tg-destructive bg-tg-secondary-bg rounded-md hover:opacity-90 transition-opacity"
            >
              <Trash2 className="w-4 h-4 mr-1" />
              Delete
            </button>
          </div>

          {/* Tasks */}
          {tasks.length > 0 && (
            <div className="pt-4">
              <h3 className="text-lg font-medium text-tg-text mb-3">Tasks ({tasks.length})</h3>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-tg-hint">
                      <th className="text-center py-2 px-4">Status</th>
                      <th className="text-left py-2 px-4">Frames</th>
                      <th className="text-center py-2 px-4">Progress</th>
                      <th className="text-center py-2 px-4">Time</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tasks.map((task) => (
                      <tr key={task._id} className="border-t border-tg-secondary-bg">
                        <td className="py-2 px-4 text-center">
                          <span className={`font-medium ${getStatusColor(task.Stat)}`}>
                            {getTaskIcon(task.Stat)}
                          </span>
                        </td>
                        <td className="py-2 px-4 text-tg-text">
                          {task.Frames || task.Props.Name}
                        </td>
                        <td className="py-2 px-4 text-center text-tg-text">
                          {task.Prog || '0%'}
                        </td>
                        <td className="py-2 px-4 text-center text-tg-text">
                          {formatTaskTime(task)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}; 