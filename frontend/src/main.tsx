import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

// StrictMode disabled — incompatible with Cesium Viewer lifecycle
// (double-mount destroys viewer while async tile loads are in-flight)
createRoot(document.getElementById('root')!).render(<App />)
