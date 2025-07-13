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
    case 3: return 'text-blue-600'; // Completed
    case 4: return 'text-red-600'; // Failed
    case 6: return 'text-gray-600'; // Pending
    default: return 'text-gray-500';
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
      <div className="bg-white rounded-lg max-w-4xl w-full max-h-[90vh] overflow-y-auto">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-gray-200">
          <div>
            <h2 className="text-xl font-semibold text-gray-900">{job.Props.Name}</h2>
            <p className="text-sm text-gray-500 mt-1">ID: {job._id}</p>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 transition-colors"
          >
            <X className="w-6 h-6" />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-6">
          {/* Job Info */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <h3 className="text-lg font-medium text-gray-900 mb-3">Информация о задаче</h3>
              <div className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-gray-600">Батч:</span>
                  <span className="font-medium">{job.Props.Batch}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-600">Пользователь:</span>
                  <span className="font-medium">{job.Props.User}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-600">Статус:</span>
                  <span className={`font-medium ${getStatusColor(job.Stat)}`}>
                    {getStatusIcon(job.Stat)} {getStatusText(job.Stat)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-600">Дата создания:</span>
                  <span className="font-medium">{new Date(job.Date).toLocaleString()}</span>
                </div>
                {job.Props.Comment && (
                  <div className="flex justify-between">
                    <span className="text-gray-600">Комментарий:</span>
                    <span className="font-medium">{job.Props.Comment}</span>
                  </div>
                )}
              </div>
            </div>

            <div>
              <h3 className="text-lg font-medium text-gray-900 mb-3">Прогресс</h3>
              <div className="space-y-3">
                <div className="flex justify-between text-sm">
                  <span className="text-gray-600">Выполнено:</span>
                  <span className="font-medium">{job.CompletedChunks} / {job.Props.Tasks}</span>
                </div>
                <div className="w-full bg-gray-200 rounded-full h-3">
                  <div
                    className="bg-primary-600 h-3 rounded-full transition-all duration-300"
                    style={{ width: `${progress}%` }}
                  />
                </div>
                <div className="text-center text-sm font-medium text-gray-700">
                  {progress}%
                </div>
              </div>
            </div>
          </div>

          {/* Actions */}
          <div className="border-t border-gray-200 pt-4">
            <h3 className="text-lg font-medium text-gray-900 mb-3">Действия</h3>
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
                className="flex items-center px-4 py-2 text-sm font-medium text-blue-700 bg-blue-100 rounded-md hover:bg-blue-200 transition-colors disabled:opacity-50"
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
                  <div className="w-full border-t border-gray-200 my-2" />
                  <div className="w-full">
                    <h4 className="text-sm font-medium text-gray-700 mb-2">Preview</h4>
                  </div>
                  
                  {onDownloadFiles && (
                    <button
                      onClick={() => onDownloadFiles(job._id)}
                      disabled={previewLoading}
                      className="flex items-center px-4 py-2 text-sm font-medium text-purple-700 bg-purple-100 rounded-md hover:bg-purple-200 transition-colors disabled:opacity-50"
                    >
                      <Eye className="w-4 h-4 mr-2" />
                      Скачать файлы
                    </button>
                  )}
                  
                  {onCreateVideo && (
                    <button
                      onClick={() => onCreateVideo(job._id)}
                      disabled={previewLoading}
                      className="flex items-center px-4 py-2 text-sm font-medium text-indigo-700 bg-indigo-100 rounded-md hover:bg-indigo-200 transition-colors disabled:opacity-50"
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
            <div className="border-t border-gray-200 pt-4">
              <h3 className="text-lg font-medium text-gray-900 mb-3">Задачи ({tasks.length})</h3>
              <div className="space-y-2 max-h-60 overflow-y-auto">
                {tasks.map((task) => (
                  <div key={task._id} className="flex items-center justify-between p-3 bg-gray-50 rounded-md">
                    <div className="flex-1">
                      <p className="text-sm font-medium text-gray-900">{task.Props.Name}</p>
                      <p className="text-xs text-gray-500">ID: {task._id}</p>
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