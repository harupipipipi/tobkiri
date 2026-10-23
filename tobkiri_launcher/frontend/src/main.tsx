import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import App from './App.tsx';
import { ErrorBoundary } from './components/ui/ErrorBoundary.tsx';
import { bootstrapDocumentAppearance } from './lib/appearance.ts';
import './index.css';

performance.mark('tobkiri:app-entry');
bootstrapDocumentAppearance();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
