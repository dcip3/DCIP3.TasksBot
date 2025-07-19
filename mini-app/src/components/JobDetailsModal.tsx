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
  onDownloadFiles?: (jobId: string) => void;
  onCreateVideo?: (jobId: string) => void;
  loading?: boolean;
  previewLoading?: boolean;
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

export const JobDetailsModal: React.FC<JobDetailsModalProps> = ({
  job,
  tasks,
  isOpen,
  onClose,
  onRequeue,
  onResume,
  onSuspend,
  onDelete,
  onDownloadFiles,
  onCreateVideo,
  loading = false,
  previewLoading = false,
}) => {
  if (!isOpen || !job) return null;

  const progress = job.Props.Tasks > 0 
    ? Math.round((job.CompletedChunks / job.Props.Tasks) * 100)
    : 0;

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
      <div className="bg-telegram-white rounded-lg max-w-4xl w-full max-h-[90vh] overflow-y-auto">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-telegram-secondary">
          <div>
            <h2 className="text-xl font-semibold text-telegram-dark">{job.Props.Name}</h2>
            <p className="text-sm text-telegram-gray mt-1">ID: {job._id}</p>
          </div>
          <button
            onClick={onClose}
            className="text-telegram-gray hover:text-telegram-dark transition-colors"
          >
            <X className="w-6 h-6" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-6">
          {/* Job Info */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <h3 className="text-lg font-medium text-telegram-dark mb-3">Информация о задаче</h3>
              <div className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-telegram-gray">Батч:</span>
                  <span className="font-medium">{job.Props.Batch}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-telegram-gray">Пользователь:</span>
                  <span className="font-medium">{job.Props.User}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-telegram-gray">Статус:</span>
                  <span className={`font-medium ${getStatusColor(job.Stat)}`}>
                    {getStatusIcon(job.Stat)} {getStatusText(job.Stat)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-telegram-gray">Дата создания:</span>
                  <span className="font-medium">{new Date(job.Date).toLocaleString()}</span>
                </div>
                {job.Props.Comment && (
                  <div className="flex justify-between">
                    <span className="text-telegram-gray">Комментарий:</span>
                    <span className="font-medium">{job.Props.Comment}</span>
                  </div>
                )}
              </div>
            </div>

            <div>
              <h3 className="text-lg font-medium text-telegram-dark mb-3">Прогресс</h3>
              <div className="space-y-3">
                <div className="flex justify-between text-sm">
                  <span className="text-telegram-gray">Выполнено:</span>
                  <span className="font-medium">{job.CompletedChunks} / {job.Props.Tasks}</span>
                </div>
                <div className="w-full bg-telegram-secondary rounded-full h-3">
                  <div
                    className="bg-telegram-primary h-3 rounded-full transition-all duration-300"
                    style={{ width: `${progress}%` }}
                  />
                </div>
                <div className="text-center text-sm font-medium text-telegram-dark">
                  {progress}%
                </div>
              </div>
            </div>
          </div>

          {/* Actions */}
          <div className="border-t border-telegram-secondary pt-4">
            <h3 className="text-lg font-medium text-telegram-dark mb-3">Действия</h3>
            <div className="flex flex-wrap gap-2">
              {job.Stat === 2 && (
                <button
                  onClick={() => onResume(job._id)}
                  disabled={loading}
                  className="flex items-center px-4 py-2 text-sm font-medium text-green-700 bg-green-100 rounded-md hover:bg-green-200 transition-colors disabled:opacity-50"
                >
                  <Play className="w-4 h-4 mr-2" />
                  Возобновить
                </button>
              )}
              
              {job.Stat === 1 && (
                <button
                  onClick={() => onSuspend(job._id)}
                  disabled={loading}
                  className="flex items-center px-4 py-2 text-sm font-medium text-yellow-700 bg-yellow-100 rounded-md hover:bg-yellow-200 transition-colors disabled:opacity-50"
                >
                  <Pause className="w-4 h-4 mr-2" />
                  Приостановить
                </button>
              )}
              
              <button
                onClick={() => onRequeue(job._id)}
                disabled={loading}
                className="flex items-center px-4 py-2 text-sm font-medium text-telegram-primary bg-telegram-accent rounded-md hover:bg-telegram-primary hover:text-telegram-white transition-colors disabled:opacity-50"
              >
                <RotateCcw className="w-4 h-4 mr-2" />
                Перезапустить
              </button>
              
              <button
                onClick={() => onDelete(job._id)}
                disabled={loading}
                className="flex items-center px-4 py-2 text-sm font-medium text-red-700 bg-red-100 rounded-md hover:bg-red-200 transition-colors disabled:opacity-50"
              >
                <Trash2 className="w-4 h-4 mr-2" />
                Удалить
              </button>
              
              {/* Preview Actions */}
              {(onDownloadFiles || onCreateVideo) && (
                <>
                  <div className="w-full border-t border-telegram-secondary my-2" />
                  <div className="w-full">
                    <h4 className="text-sm font-medium text-telegram-dark mb-2">Preview</h4>
                  </div>
                  
                  {onDownloadFiles && (
                    <button
                      onClick={() => onDownloadFiles(job._id)}
                      disabled={previewLoading}
                      className="flex items-center px-4 py-2 text-sm font-medium text-telegram-accent bg-telegram-secondary rounded-md hover:bg-telegram-accent hover:text-telegram-white transition-colors disabled:opacity-50"
                    >
                      <Eye className="w-4 h-4 mr-2" />
                      Скачать файлы
                    </button>
                  )}
                  
                  {onCreateVideo && (
                    <button
                      onClick={() => onCreateVideo(job._id)}
                      disabled={previewLoading}
                      className="flex items-center px-4 py-2 text-sm font-medium text-telegram-primary bg-telegram-accent rounded-md hover:bg-telegram-primary hover:text-telegram-white transition-colors disabled:opacity-50"
                    >
                      <Eye className="w-4 h-4 mr-2" />
                      Создать видео
                    </button>
                  )}
                </>
              )}
            </div>
          </div>

          {/* Tasks */}
          {tasks.length > 0 && (
            <div className="border-t border-telegram-secondary pt-4">
              <h3 className="text-lg font-medium text-telegram-dark mb-3">Задачи ({tasks.length})</h3>
              <div className="space-y-2 max-h-60 overflow-y-auto">
                {tasks.map((task) => (
                  <div key={task._id} className="flex items-center justify-between p-3 bg-telegram-secondary rounded-md">
                    <div className="flex-1">
                      <p className="text-sm font-medium text-telegram-dark">{task.Props.Name}</p>
                      <p className="text-xs text-telegram-gray">ID: {task._id}</p>
                    </div>
                    <div className="flex items-center space-x-2">
                      <span className="text-lg">{getStatusIcon(task.Stat)}</span>
                      <span className={`text-xs font-medium ${getStatusColor(task.Stat)}`}>
                        {getStatusText(task.Stat)}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}; 