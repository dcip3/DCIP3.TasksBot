import React, { useState, useEffect } from 'react';
import { LoginForm } from './components/LoginForm';
import { Navigation } from './components/Navigation';
import { JobCard } from './components/JobCard';
import { WorkerCard } from './components/WorkerCard';
import { initTelegramApp, getTelegramUser, getTelegramThemeParams, subscribeThemeChanged } from './utils/telegram';
import { jobsApi, workersApi, authApi } from './services/api';
import { Job, Worker, User } from './types';

function App() {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loginLoading, setLoginLoading] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  const [activeTab, setActiveTab] = useState<'jobs' | 'workers'>('jobs');
  const [jobs, setJobs] = useState<Job[]>([]);
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    console.log('App mounted');
    initTelegramApp();
    checkAuth();

    // --- THEME INIT ---
    const applyTheme = (themeParams: any) => {
      if (!themeParams) return;
      const root = document.documentElement;
      // Apply each theme parameter as a CSS custom property
      Object.entries(themeParams).forEach(([key, value]) => {
        // Transform accent_text_color -> --tg-theme-accent-text-color
        const cssVar = '--tg-theme-' + key.replace(/_/g, '-');
        root.style.setProperty(cssVar, value);
      });

      // Update body background color
      document.body.style.backgroundColor = themeParams.bg_color || '#ffffff';
    };
    // Apply theme on startup
    const themeParams = getTelegramThemeParams();
    if (themeParams) {
      applyTheme(themeParams);
    }
    // Subscribe to theme change events
    subscribeThemeChanged(applyTheme);
    // --- END THEME INIT ---
  }, []);

  const checkAuth = async () => {
    console.log('Checking auth...');
    try {
      const response = await authApi.checkAuth();
      console.log('Auth check response:', response);
      if (response.data.authenticated) {
        setIsAuthenticated(true);
        setUser(response.data.user);
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
      } else {
        setError(response.data.message || 'Invalid login or password');
      }
    } catch (error: any) {
      console.error('Login error:', error);
      setError(error.response?.data?.detail || 'Login error. Please try again.');
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
        const jobs = response.data as Job[];
        console.log('Jobs response:', jobs);
        
        // Group jobs by batch name
        const grouped: { [key: string]: Job[] } = {};
        jobs.forEach((job: Job) => {
          const batch = job.Props.Batch || 'Untitled';
          if (!grouped[batch]) {
            grouped[batch] = [];
          }
          grouped[batch].push(job);
        });

        // Combine jobs by batch and find max date for each batch
        const combinedJobs: (Job & { maxDate: number; batchSize: number })[] = [];
        Object.entries(grouped).forEach(([batch, batchJobs]) => {
          // Find the most recent date among jobs in this batch
          const dates = batchJobs
            .map(j => j.Date ? new Date(j.Date).getTime() : 0)
            .filter(d => d > 0);
          const maxDate = dates.length > 0 ? Math.max(...dates) : 0;

          // Determine batch-level status with priority: Active > Pending > Suspended > Failed > Completed > Unknown
          const statusList = batchJobs.map(j => j.Stat);
          let batchStat = 0;

          // If any task is active, treat the entire batch as active
          if (statusList.includes(1)) {
            batchStat = 1;  // Active
          }
          // If nothing is active but at least one pending task exists, mark batch as pending
          else if (statusList.includes(6)) {
            batchStat = 6;  // Pending
          }
          // If no active or pending tasks but a suspended one exists, mark batch as suspended
          else if (statusList.includes(2)) {
            batchStat = 2;  // Suspended
          }
          // If none are active, pending, or suspended but at least one failed, mark batch as failed
          else if (statusList.includes(4)) {
            batchStat = 4;  // Failed
          }
          // If all tasks are completed, mark batch as completed
          else if (statusList.every(s => s === 3)) {
            batchStat = 3;  // Completed
          }
          // In every other scenario mark as unknown
          else {
            batchStat = 0;  // Unknown
          }

          // Calculate total tasks and completed chunks
          const totalTasks = batchJobs.reduce((sum, j) => sum + (j.Props.Tasks || 0), 0);
          const completedChunks = batchJobs.reduce((sum, j) => sum + (j.CompletedChunks || 0), 0);

          // Use the first job as a base and update its properties
          const combinedJob = { 
            ...batchJobs[0],
            maxDate,
            batchSize: batchJobs.length,
            Props: {
              ...batchJobs[0].Props,
              Tasks: totalTasks
            },
            CompletedChunks: completedChunks,
            Stat: batchStat
          };

          combinedJobs.push(combinedJob);
        });

        // Sort by maxDate descending (newest first)
        const sortedJobs = combinedJobs.sort((a, b) => b.maxDate - a.maxDate);
        
        setJobs(sortedJobs);
      } else if (activeTab === 'workers') {
        console.log('Loading workers...');
        const response = await workersApi.getWorkers();
        console.log('Workers response:', response);
        if (Array.isArray(response.data)) {
          setWorkers(response.data);
        } else {
          console.error('Workers response is not an array:', response.data);
          setWorkers([]);
        }
      }
    } catch (error) {
      console.error('Error loading data:', error);
      setError('Error loading data');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (isAuthenticated) {
      loadData();
    }
  }, [activeTab, isAuthenticated]);

  const handleTabChange = (tab: 'jobs' | 'workers') => {
    setActiveTab(tab);
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
      loadData();
    } catch (error) {
      setError('Error performing action');
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-tg-bg">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-tg-button"></div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return <LoginForm onLogin={handleLogin} loading={loginLoading} />;
  }

  // Separate jobs by status
  const activeJobs = jobs.filter(job => job.Stat === 1);
  const suspendedJobs = jobs.filter(job => job.Stat === 2);
  const otherJobs = jobs.filter(job => job.Stat !== 1 && job.Stat !== 2);

  return (
    <div className="min-h-screen bg-tg-bg">
      <Navigation
        activeTab={activeTab}
        onTabChange={handleTabChange}
        onRefresh={loadData}
        onLogout={handleLogout}
        loading={loading}
      />
      
      {error && (
        <div className="p-4 bg-red-100 text-red-700 text-sm">
          {error}
        </div>
      )}

      <div className="p-4 space-y-4">
        {activeTab === 'jobs' && (
          <>
            {/* Active Jobs */}
            {activeJobs.length > 0 && (
              <div className="space-y-4">
                {activeJobs.map(job => (
                  <JobCard
                    key={job._id}
                    job={job}
                    onRequeue={(id) => handleJobAction(id, 'requeue')}
                    onResume={(id) => handleJobAction(id, 'resume')}
                    onSuspend={(id) => handleJobAction(id, 'suspend')}
                    onDelete={(id) => handleJobAction(id, 'delete')}
                  />
                ))}
              </div>
            )}

            {/* Suspended Jobs */}
            {suspendedJobs.length > 0 && (
              <div className="space-y-4">
                <div className="text-center text-tg-hint py-2 border-t border-b border-tg-secondary-bg">
                  Suspended Jobs
                </div>
                {suspendedJobs.map(job => (
                  <JobCard
                    key={job._id}
                    job={job}
                    onRequeue={(id) => handleJobAction(id, 'requeue')}
                    onResume={(id) => handleJobAction(id, 'resume')}
                    onSuspend={(id) => handleJobAction(id, 'suspend')}
                    onDelete={(id) => handleJobAction(id, 'delete')}
                  />
                ))}
              </div>
            )}

            {/* Other Jobs */}
            {otherJobs.length > 0 && (
              <div className="space-y-4">
                {otherJobs.map(job => (
                  <JobCard
                    key={job._id}
                    job={job}
                    onRequeue={(id) => handleJobAction(id, 'requeue')}
                    onResume={(id) => handleJobAction(id, 'resume')}
                    onSuspend={(id) => handleJobAction(id, 'suspend')}
                    onDelete={(id) => handleJobAction(id, 'delete')}
                  />
                ))}
              </div>
            )}

            {jobs.length === 0 && !loading && (
              <div className="text-center text-tg-hint py-8">
                No jobs found
              </div>
            )}
          </>
        )}
        
        {activeTab === 'workers' && workers.map(worker => (
          <WorkerCard
            key={worker.Info.Name}
            worker={worker}
          />
        ))}
        
        {activeTab === 'workers' && workers.length === 0 && !loading && (
          <div className="text-center text-tg-hint py-8">
            No workers found
          </div>
        )}
      </div>
    </div>
  );
}

export default App; 
