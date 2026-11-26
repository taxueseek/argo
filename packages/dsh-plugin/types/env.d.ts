// Ambient type shims for zero-dependency checkJs validation of @taxueseek/argo-dsh.
// Not included in npm package "files"; zero runtime footprint.

declare const process: any;
declare const Buffer: any;

interface Error {
  code?: string;
}

interface Window {
  __ModuleLoader__?: {
    load(def: { id: string; factory: (require: (id: string) => any) => any }): void;
  };
}

declare function setTimeout(callback: (...args: any[]) => void, ms?: number, ...args: any[]): any;
declare function setInterval(callback: (...args: any[]) => void, ms?: number, ...args: any[]): any;
declare function clearTimeout(id?: any): void;
declare function clearInterval(id?: any): void;

declare module 'react';
declare module 'node:*';
