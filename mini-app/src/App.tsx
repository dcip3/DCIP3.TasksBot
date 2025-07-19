import React, { useState, useEffect } from 'react';
import { LoginForm } from './components/LoginForm';
import { Navigation } from './components/Navigation';
import { JobCard } from './components/JobCard';
import { WorkerCard } from './components/WorkerCard';
import { JobDetailsModal } from './components/JobDetailsModal';
import { initTelegramApp, getTelegramUser, getTelegramThemeParams, subscribeThemeChanged } from './utils/telegram';
import { jobsApi, workersApi, authApi } from './services/api';
import { Job, Worker, User, Task } from './types';

function App() {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loginLoading, setLoginLoading] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  const [activeTab, setActiveTab] = useState<'jobs' | 'workers'>('jobs');
  const [jobs, setJobs] = useState<Job[]>([]);
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notification, setNotification] = useState<{ type: 'success' | 'error'; message: string } | null>(null);
  
  // Modal state
  const [selectedJob, setSelectedJob] = useState<Job | null>(null);
  const [jobTasks, setJobTasks] = useState<Task[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [modalLoading, setModalLoading] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);

  useEffect(() => {
    console.log('App mounted');
    initTelegramApp();
    checkAuth();

    // --- THEME INIT ---
    const applyTheme = (themeParams: any) => {
      if (!themeParams) return;
      const root = document.documentElement;
      // Применяем все параметры как CSS custom properties
      Object.entries(themeParams).forEach(([key, value]) => {
        // Преобразуем accent_text_color -> --tg-theme-accent-text-color
        const cssVar = '--tg-theme-' + key.replace(/_/g, '-');
        root.style.setProperty(cssVar, value);
      });
    };
    // Применяем тему при запуске
    const themeParams = getTelegramThemeParams();
    if (themeParams) {
      applyTheme(themeParams);
    }
    // Подписываемся на смену темы
    subscribeThemeChanged(applyTheme);
    // --- END THEME INIT ---
  }, []);

  useEffect(() => {
    console.log('Workers state changed:', workers.length);
    console.log('Workers data:', workers);
  }, [workers]);

  const checkAuth = async () => {
    console.log('Checking auth...');
    try {
      const response = await authApi.checkAuth();
      console.log('Auth check response:', response);
      if (response.data.authenticated) {
        setIsAuthenticated(true);
        setUser(response.data.user);
        // loadData будет вызван автоматически через useEffect
      }
    } catch (error) {
      console.error('Auth check failed:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleLogin = async (username: string, password: string) => {
    console.log('Login attempt:', { username, password: '***' });
    setLoginLoading(true);
    setError(null);
    
    try {
      console.log('Sending login request...');
      const response = await authApi.login(username, password);
      console.log('Login response:', response);
      
      if (response.data.success) {
        setIsAuthenticated(true);
        setUser(response.data.user);
        // loadData будет вызван автоматически через useEffect
      } else {
        setError(response.data.message || 'Неверный логин или пароль');
      }
    } catch (error: any) {
      console.error('Login error:', error);
      setError(error.response?.data?.detail || 'Ошибка входа. Попробуйте еще раз.');
    } finally {
      setLoginLoading(false);
    }
  };

  const handleLogout = async () => {
    try {
      await authApi.logout();
    } catch (error) {
      console.error('Logout error:', error);
    } finally {
      setIsAuthenticated(false);
      setUser(null);
      setJobs([]);
      setWorkers([]);
    }
  };

  const loadData = async () => {
    console.log('loadData called, activeTab:', activeTab);
    setLoading(true);
    try {
      if (activeTab === 'jobs') {
        console.log('Loading jobs...');
        const response = await jobsApi.getJobs();
        console.log('Jobs response:', response.data);
        setJobs(response.data);
      } else if (activeTab === 'workers') {
        console.log('Loading workers...');
        console.log('Making request to /api/slaves...');
        const response = await workersApi.getWorkers();
        console.log('Workers response:', response);
        console.log('Workers data:', response.data);
        console.log('Workers length:', Array.isArray(response.data) ? response.data.length : 'Not an array');
        console.log('Workers type:', typeof response.data);
        if (Array.isArray(response.data)) {
          setWorkers(response.data);
        } else {
          console.error('Workers response is not an array:', response.data);
          setWorkers([]);
        }
      }
    } catch (error) {
      console.error('Error loading data:', error);
      setError('Ошибка загрузки данных');
    } finally {
      setLoading(false);
    }
  };

  // useEffect для загрузки данных при изменении вкладки
  useEffect(() => {
    console.log('ActiveTab changed to:', activeTab);
    // Загружаем данные при изменении вкладки
    if (isAuthenticated) {
      console.log('Loading data for tab:', activeTab);
      loadData();
    }
  }, [activeTab, isAuthenticated]);

  const handleTabChange = (tab: 'jobs' | 'workers') => {
    console.log('=== handleTabChange called ===');
    console.log('Tab changed to:', tab);
    console.log('Previous activeTab:', activeTab);
    setActiveTab(tab);
    // loadData будет вызван автоматически через useEffect
  };

  const handleJobAction = async (jobId: string, action: 'requeue' | 'resume' | 'suspend' | 'delete') => {
    try {
      switch (action) {
        case 'requeue':
          await jobsApi.requeueJob(jobId);
          break;
        case 'resume':
          await jobsApi.resumeJob(jobId);
          break;
        case 'suspend':
          await jobsApi.suspendJob(jobId);
          break;
        case 'delete':
          await jobsApi.deleteJob(jobId);
          break;
      }
      loadData(); // Перезагружаем данные после действия
    } catch (error) {
      setError('Ошибка выполнения действия');
    }
  };

  const handleViewJobDetails = async (jobId: string) => {
    try {
      setModalLoading(true);
      
      // Найти задачу в списке
      const job = jobs.find(j => j._id === jobId);
      if (!job) {
        setError('Задача не найдена');
        return;
      }
      
      setSelectedJob(job);
      
      // Загрузить задачи для этой job
      try {
        const tasksResponse = await jobsApi.getJobTasks(jobId);
        setJobTasks(tasksResponse.data);
      } catch (error) {
        console.error('Error loading tasks:', error);
        setJobTasks([]);
      }
      
      setModalOpen(true);
    } catch (error) {
      console.error('Error opening job details:', error);
      setError('Ошибка загрузки деталей задачи');
    } finally {
      setModalLoading(false);
    }
  };

  const closeModal = () => {
    setModalOpen(false);
    setSelectedJob(null);
    setJobTasks([]);
  };

  const handleDownloadFiles = async (jobId: string) => {
    try {
      setPreviewLoading(true);
      setError(null);
      setNotification(null);
      
      const response = await jobsApi.downloadJobFiles(jobId);
      console.log('Download files response:', response.data);
      
      if (response.data.success) {
        setNotification({ type: 'success', message: 'Файлы успешно скачаны!' });
      } else {
        setNotification({ type: 'error', message: 'Ошибка скачивания файлов' });
      }
    } catch (error: any) {
      console.error('Download files error:', error);
      setNotification({ type: 'error', message: error.response?.data?.detail || 'Ошибка скачивания файлов' });
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleCreateVideo = async (jobId: string) => {
    try {
      setPreviewLoading(true);
      setError(null);
      setNotification(null);
      
      const response = await jobsApi.createJobVideo(jobId);
      console.log('Create video response:', response.data);
      
      if (response.data.success) {
        setNotification({ type: 'success', message: 'Видео успешно создано!' });
        
        // Обновляем информацию о задаче с видео
        const updatedJobs = jobs.map(job => {
          if (job._id === jobId) {
            return {
              ...job,
              video_path: response.data.video_path,
              video_dropbox_path: response.data.video_dropbox_path
            };
          }
          return job;
        });
        setJobs(updatedJobs);
      } else {
        setNotification({ type: 'error', message: 'Ошибка создания видео' });
      }
    } catch (error: any) {
      console.error('Create video error:', error);
      setNotification({ type: 'error', message: error.response?.data?.detail || 'Ошибка создания видео' });
    } finally {
      setPreviewLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600"></div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return (
      <LoginForm 
        onLogin={handleLogin} 
        loading={loginLoading}
      />
    );
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <Navigation
        activeTab={activeTab}
        onTabChange={handleTabChange}
        onRefresh={loadData}
        onLogout={handleLogout}
        loading={loading}
      />
      
      <div className="p-4">
        {error && (
          <div className="mb-4 p-3 bg-red-100 border border-red-400 text-red-700 rounded-md">
            {error}
          </div>
        )}
        
        {notification && (
          <div className={`mb-4 p-3 border rounded-md ${
            notification.type === 'success' 
              ? 'bg-green-100 border-green-400 text-green-700' 
              : 'bg-red-100 border-red-400 text-red-700'
          }`}>
            <div className="flex justify-between items-center">
              <span>{notification.message}</span>
              <button 
                onClick={() => setNotification(null)}
                className="text-gray-500 hover:text-gray-700"
              >
                ✕
              </button>
            </div>
          </div>
        )}
        
        {activeTab === 'jobs' ? (
          <div className="space-y-4">
            {jobs.length === 0 ? (
              <div className="text-center py-8 text-gray-500">
                Задачи не найдены
              </div>
            ) : (
              jobs.map((job) => (
                <JobCard
                  key={job._id}
                  job={job}
                  onViewDetails={handleViewJobDetails}
                  onRequeue={(jobId) => handleJobAction(jobId, 'requeue')}
                  onResume={(jobId) => handleJobAction(jobId, 'resume')}
                  onSuspend={(jobId) => handleJobAction(jobId, 'suspend')}
                  onDelete={(jobId) => handleJobAction(jobId, 'delete')}
                />
              ))
            )}
          </div>
        ) : (
          <div className="space-y-4">
            {workers.length === 0 ? (
              <div className="text-center py-8 text-gray-500">
                Воркеры не найдены
              </div>
            ) : (
              workers.map((worker) => (
                <WorkerCard key={worker._id} worker={worker} />
              ))
            )}
          </div>
        )}
      </div>

      {/* Job Details Modal */}
      <JobDetailsModal
        job={selectedJob}
        tasks={jobTasks}
        isOpen={modalOpen}
        onClose={closeModal}
        onRequeue={(jobId) => {
          handleJobAction(jobId, 'requeue');
          closeModal();
        }}
        onResume={(jobId) => {
          handleJobAction(jobId, 'resume');
          closeModal();
        }}
        onSuspend={(jobId) => {
          handleJobAction(jobId, 'suspend');
          closeModal();
        }}
        onDelete={(jobId) => {
          handleJobAction(jobId, 'delete');
          closeModal();
        }}
        onDownloadFiles={handleDownloadFiles}
        onCreateVideo={handleCreateVideo}
        loading={modalLoading}
        previewLoading={previewLoading}
      />
    </div>
  );
}

export default App; 