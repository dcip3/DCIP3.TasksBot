export interface Job {
  _id: string;
  Props: {
    Name: string;
    Batch: string;
    Tasks: number;
    User: string;
    Comment?: string;
  };
  Stat: number;
  CompletedChunks: number;
  Date: string;
  video_path?: string;
  video_dropbox_path?: string;
}

export interface Worker {
  _id: string;
  Info: {
    Name: string;
    IP: string;
    MAC: string;
    User: string;
    Stat: number;
    StatDate: string;
    Host: string;
    OS: string;
    Ver: string;
    RAM: number;
    RAMFree: number;
    CPU: number;
    Procs: number;
    UpTime: number;
  };
  Settings: {
    Name: string;
    Enable: boolean;
    Pools: string[];
    Grps: string[];
  };
}

export interface Task {
  _id: string;
  Props: {
    Name: string;
    Status: number;
  };
  Stat: number;
  Date: string;
}

export interface User {
  id: number;
  username?: string;
  first_name?: string;
  last_name?: string;
}

export interface AuthState {
  isAuthenticated: boolean;
  user: User | null;
  loading: boolean;
} 