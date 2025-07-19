import React, { useState } from 'react';
import { Job } from '../types';
import { Play, Pause, RotateCcw, Trash2, Eye, Video } from 'lucide-react';

interface JobCardProps {
  job: Job;
  onViewDetails: (jobId: string) => void;
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
    case 1: return 'text-green-600'; // Active
    case 2: return 'text-yellow-600'; // Suspended
    case 3: return 'text-telegram-primary'; // Completed
    case 4: return 'text-red-600'; // Failed
    case 6: return 'text-telegram-gray'; // Pending
    default: return 'text-telegram-gray';
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

export const JobCard: React.FC<JobCardProps> = ({
  job,
  onViewDetails,
  onRequeue,
  onResume,
  onSuspend,
  onDelete,
}) => {
  const [showVideo, setShowVideo] = useState(false);
  const progress = job.Props.Tasks > 0 
    ? Math.round((job.CompletedChunks / job.Props.Tasks) * 100)
    : 0;

  return (
    <div className="bg-telegram-white rounded-lg shadow-sm border border-telegram-secondary p-4 space-y-3">
      <div className="flex items-start justify-between">
        <div className="flex-1 min-w-0">
          <h3 className="text-sm font-medium text-telegram-dark truncate">
            {job.Props.Name}
          </h3>
          <p className="text-xs text-telegram-gray mt-1">
            Батч: {job.Props.Batch}
          </p>
          <p className="text-xs text-telegram-gray">
            Пользователь: {job.Props.User}
          </p>
        </div>
        <div className="flex items-center space-x-1 ml-2">
          <span className="text-lg">{getStatusIcon(job.Stat)}</span>
          <span className={`text-xs font-medium ${getStatusColor(job.Stat)}`}>
            {getStatusText(job.Stat)}
          </span>
        </div>
      </div>

      <div className="space-y-2">
        <div className="flex justify-between text-xs text-telegram-gray">
          <span>Прогресс</span>
          <span>{progress}% ({job.CompletedChunks}/{job.Props.Tasks})</span>
        </div>
        <div className="w-full bg-telegram-secondary rounded-full h-2">
          <div
            className="bg-telegram-primary h-2 rounded-full transition-all duration-300"
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>

      <div className="flex space-x-2 pt-2">
        <button
          onClick={() => onViewDetails(job._id)}
          className="flex-1 flex items-center justify-center px-3 py-2 text-xs font-medium text-telegram-dark bg-telegram-secondary rounded-md hover:bg-telegram-accent hover:text-telegram-white transition-colors"
        >
          <Eye className="w-4 h-4 mr-1" />
          Детали
        </button>
        
        {job.Stat === 2 && (
          <button
            onClick={() => onResume(job._id)}
            className="flex-1 flex items-center justify-center px-3 py-2 text-xs font-medium text-green-700 bg-green-100 rounded-md hover:bg-green-200 transition-colors"
          >
            <Play className="w-4 h-4 mr-1" />
            Возобновить
          </button>
        )}
        
        {job.Stat === 1 && (
          <button
            onClick={() => onSuspend(job._id)}
            className="flex-1 flex items-center justify-center px-3 py-2 text-xs font-medium text-yellow-700 bg-yellow-100 rounded-md hover:bg-yellow-200 transition-colors"
          >
            <Pause className="w-4 h-4 mr-1" />
            Приостановить
          </button>
        )}
        
        <button
          onClick={() => onRequeue(job._id)}
          className="flex-1 flex items-center justify-center px-3 py-2 text-xs font-medium text-telegram-primary bg-telegram-accent rounded-md hover:bg-telegram-primary hover:text-telegram-white transition-colors"
        >
          <RotateCcw className="w-4 h-4 mr-1" />
          Перезапустить
        </button>
        
        <button
          onClick={() => onDelete(job._id)}
          className="flex-1 flex items-center justify-center px-3 py-2 text-xs font-medium text-red-700 bg-red-100 rounded-md hover:bg-red-200 transition-colors"
        >
          <Trash2 className="w-4 h-4 mr-1" />
          Удалить
        </button>
      </div>

      {/* Video Section */}
      {job.video_path && (
        <div className="border-t border-telegram-secondary pt-3">
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-xs font-medium text-telegram-dark">Видео</h4>
            <button
              onClick={() => setShowVideo(!showVideo)}
              className="flex items-center px-2 py-1 text-xs font-medium text-telegram-accent bg-telegram-secondary rounded-md hover:bg-telegram-accent hover:text-telegram-white transition-colors"
            >
              <Video className="w-3 h-3 mr-1" />
              {showVideo ? 'Скрыть' : 'Показать'}
            </button>
          </div>
          
          {showVideo && (
            <div className="bg-telegram-white rounded-md p-3">
              <video
                controls
                className="w-full rounded-md"
                style={{ maxHeight: '200px' }}
              >
                <source src={`http://localhost:8000/api/video/${encodeURIComponent(job.video_path)}`} type="video/mp4" />
                Ваш браузер не поддерживает видео.
              </video>
              {job.video_dropbox_path && (
                <p className="text-xs text-telegram-gray mt-2">
                  Путь в Dropbox: {job.video_dropbox_path}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}; 