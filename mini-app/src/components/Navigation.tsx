import React from 'react';
import { Briefcase, Users, LogOut, RefreshCw } from 'lucide-react';

interface NavigationProps {
  activeTab: 'jobs' | 'workers';
  onTabChange: (tab: 'jobs' | 'workers') => void;
  onRefresh: () => void;
  onLogout: () => void;
  loading?: boolean;
}

export const Navigation: React.FC<NavigationProps> = ({
  activeTab,
  onTabChange,
  onRefresh,
  onLogout,
  loading = false,
}) => {
  return (
    <div className="bg-white border-b border-gray-200">
      <div className="flex items-center justify-between px-4 py-3">
        <div className="flex space-x-1">
          <button
            onClick={() => {
              console.log('Jobs tab clicked');
              onTabChange('jobs');
            }}
            className={`flex items-center px-3 py-2 text-sm font-medium rounded-md transition-colors ${
              activeTab === 'jobs'
                ? 'bg-primary-100 text-primary-700'
                : 'text-gray-500 hover:text-gray-700 hover:bg-gray-100'
            }`}
          >
            <Briefcase className="w-4 h-4 mr-2" />
            Задачи
          </button>
          <button
            onClick={() => {
              console.log('Workers tab clicked');
              onTabChange('workers');
            }}
            className={`flex items-center px-3 py-2 text-sm font-medium rounded-md transition-colors ${
              activeTab === 'workers'
                ? 'bg-primary-100 text-primary-700'
                : 'text-gray-500 hover:text-gray-700 hover:bg-gray-100'
            }`}
          >
            <Users className="w-4 h-4 mr-2" />
            Воркеры
          </button>
        </div>
        
        <div className="flex items-center space-x-2">
          <button
            onClick={onRefresh}
            disabled={loading}
            className="p-2 text-gray-500 hover:text-gray-700 hover:bg-gray-100 rounded-md transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button
            onClick={onLogout}
            className="flex items-center px-3 py-2 text-sm font-medium text-red-600 hover:text-red-700 hover:bg-red-50 rounded-md transition-colors"
          >
            <LogOut className="w-4 h-4 mr-2" />
            Выйти
          </button>
        </div>
      </div>
    </div>
  );
}; 