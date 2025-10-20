import React from 'react';
import { Briefcase, Users, RefreshCw } from 'lucide-react';

interface NavigationProps {
  activeTab: 'jobs' | 'workers';
  onTabChange: (tab: 'jobs' | 'workers') => void;
  onRefresh: () => void;
  loading?: boolean;
}

export const Navigation: React.FC<NavigationProps> = ({
  activeTab,
  onTabChange,
  onRefresh,
  loading = false,
}) => {
  return (
    <div className="bg-tg-header border-b border-tg-secondary-bg">
      <div className="flex items-center justify-between px-4 py-3">
        <div className="flex space-x-1">
          <button
            onClick={() => {
              console.log('Jobs tab clicked');
              onTabChange('jobs');
            }}
            className={`flex items-center px-3 py-2 text-sm font-medium rounded-md transition-colors ${
              activeTab === 'jobs'
                ? 'bg-tg-button text-tg-button-text'
                : 'text-tg-hint hover:text-tg-base hover:bg-tg-secondary-bg'
            }`}
          >
            <Briefcase className="w-4 h-4 mr-2" />
            Jobs
          </button>
          <button
            onClick={() => {
              console.log('Workers tab clicked');
              onTabChange('workers');
            }}
            className={`flex items-center px-3 py-2 text-sm font-medium rounded-md transition-colors ${
              activeTab === 'workers'
                ? 'bg-tg-button text-tg-button-text'
                : 'text-tg-hint hover:text-tg-base hover:bg-tg-secondary-bg'
            }`}
          >
            <Users className="w-4 h-4 mr-2" />
            Workers
          </button>
        </div>
        
        <div className="flex items-center space-x-2">
          <button
            onClick={onRefresh}
            disabled={loading}
            className="p-2 text-tg-hint hover:text-tg-base hover:bg-tg-secondary-bg rounded-md transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>
    </div>
  );
};
