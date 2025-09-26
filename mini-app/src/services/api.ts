import axios from 'axios';
import { Job } from '../types';

const API_BASE_URL = import.meta.env.VITE_API_URL || 'https://example.com/api';

console.log('API_BASE_URL:', API_BASE_URL);

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 10000,
});

// Development mode - set to true for testing without Telegram WebApp
const DEV_MODE = true;

console.log('DEV_MODE:', DEV_MODE);

// Request interceptor that injects Telegram WebApp data
api.interceptors.request.use((config) => {
  console.log('API Request:', {
    method: config.method,
    url: config.url,
    baseURL: config.baseURL,
    fullURL: `${config.baseURL}${config.url}`,
    headers: config.headers
  });
  
  if (!DEV_MODE) {
    const tg = window.Telegram?.WebApp;
    if (tg?.initData) {
      config.headers['X-Telegram-Init-Data'] = tg.initData;
    }
  }
  return config;
});

// Response interceptor that logs responses
api.interceptors.response.use(
  (response) => {
    console.log('API Response:', {
      status: response.status,
      url: response.config.url,
      data: response.data
    });
    return response;
  },
  (error) => {
    console.error('API Error:', {
      status: error.response?.status,
      url: error.config?.url,
      message: error.message,
      data: error.response?.data
    });
    return Promise.reject(error);
  }
);

export const jobsApi = {
  getJobs: () => api.get<Job[]>('/jobs'),
  getJobInfo: (jobId: string) => api.get(`/jobs/${jobId}`),
  getJobTasks: (jobId: string) => api.get(`/jobs/${jobId}/tasks`),
  getJobsByBatch: async (batchName: string): Promise<Job[]> => {
    const response = await api.get<Job[]>('/jobs');
    return response.data.filter(job => job.Props.Batch === batchName);
  },
  requeueJob: (jobId: string) => api.put(`/jobs/${jobId}/requeue`),
  resumeJob: (jobId: string) => api.put(`/jobs/${jobId}/resume`),
  suspendJob: (jobId: string) => api.put(`/jobs/${jobId}/suspend`),
  deleteJob: (jobId: string) => api.delete(`/jobs/${jobId}`),
  downloadJobFiles: (jobId: string) => api.post(`/jobs/${jobId}/download`),
  createJobVideo: (jobId: string) => api.post(`/jobs/${jobId}/create-video`),
};

export const workersApi = {
  getWorkers: () => api.get('/slaves'),
};

export const authApi = {
  login: (username: string, password: string) => 
    api.post('/auth/login', { username, password }),
  logout: () => api.post('/auth/logout'),
  checkAuth: () => api.get('/auth/check'),
};

export default api; 
