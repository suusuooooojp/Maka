import dotenv from 'dotenv'
import express from 'express'
import mongoose from 'mongoose'
import morgan from 'morgan'
import path from 'path'
import fs from 'fs'
import https from 'https'
import { execFile } from 'child_process'

import categoriesRoutes from './modules/categories/categories.routes'
import configRoutes from './modules/config/config.routes'
import scoresRoutes from './modules/scores/scores.routes'
import songsRoutes from './modules/songs/songs.routes'

import gateway from './gateway'

// Setup .env file
dotenv.config()

// Connect to database
if (process.env['MONGODB_URI']) {
  mongoose.connect(process.env['MONGODB_URI']).then(() => {
    console.log('MongoDB database connected.')
  }).catch(err => {
    console.warn('MongoDB database connection warning (optional offline mode active):', err.message || err)
  })
} else {
  console.log('No MONGODB_URI provided. Skipping database connection.')
}

import { createProxyMiddleware } from 'http-proxy-middleware';

// Global views state
let globalViewsCount = 0;
const viewsFilePath = path.join(__dirname, '..', 'views.json');
try {
  if (fs.existsSync(viewsFilePath)) {
    globalViewsCount = JSON.parse(fs.readFileSync(viewsFilePath, 'utf8')).views || 0;
  }
} catch (e) {}

// Initialize http server
const app = express()
const PORT = process.env.PORT ? parseInt(process.env.PORT, 10) : 3000

app.use(morgan('dev'))
app.use(express.static(path.join(__dirname, '..', 'public')))

app.get('/api/views', (req, res) => {
  globalViewsCount++;
  fs.writeFile(viewsFilePath, JSON.stringify({ views: globalViewsCount }), () => {});
  res.json({ views: globalViewsCount });
});

app.use('/assets', createProxyMiddleware({ 
  target: 'https://taikoapp.uk', 
  changeOrigin: true,
  pathRewrite: (path, req) => '/assets' + path,
  on: {
    proxyRes: (proxyRes) => {
      proxyRes.headers['Access-Control-Allow-Origin'] = '*';
    }
  }
}))

const SONG_CACHE_DIR = path.join('/tmp', 'taiko_songs_cache');
try {
  if (!fs.existsSync(SONG_CACHE_DIR)) {
    fs.mkdirSync(SONG_CACHE_DIR, { recursive: true });
  }
} catch (e) {}

const inFlightTranscodes = new Map<string, Promise<string>>();

function fetchAudioBuffer(urlStr: string, redirects = 3): Promise<{ buf: Buffer; contentType?: string }> {
  return new Promise((resolve, reject) => {
    https.get(urlStr, (res) => {
      if (res.statusCode && res.statusCode >= 300 && res.statusCode < 400 && res.headers.location && redirects > 0) {
        const nextUrl = new URL(res.headers.location, urlStr).toString();
        return resolve(fetchAudioBuffer(nextUrl, redirects - 1));
      }
      if (res.statusCode !== 200) {
        return reject(new Error(`Status ${res.statusCode}`));
      }
      const chunks: Buffer[] = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({ buf: Buffer.concat(chunks), contentType: res.headers['content-type'] }));
    }).on('error', reject);
  });
}

app.use('/songs', (req, res, next) => {
  const isAudio = req.path.endsWith('.ogg') || req.path.endsWith('.mp3') || req.path.endsWith('.wav');
  if (!isAudio) {
    return next();
  }

  const safePathKey = req.path.replace(/[^a-zA-Z0-9._-]/g, '_');
  const cachedFilePath = path.join(SONG_CACHE_DIR, safePathKey);

  res.setHeader('Access-Control-Allow-Origin', '*');

  try {
    if (fs.existsSync(cachedFilePath) && fs.statSync(cachedFilePath).size > 0) {
      res.setHeader('Content-Type', 'audio/ogg');
      return res.sendFile(cachedFilePath);
    }
  } catch (e) {}

  if (inFlightTranscodes.has(safePathKey)) {
    inFlightTranscodes.get(safePathKey)!
      .then((filePath) => {
        res.setHeader('Content-Type', 'audio/ogg');
        res.sendFile(filePath);
      })
      .catch(() => next());
    return;
  }

  const upstreamUrl = 'https://taikoapp.uk/songs' + req.path;
  const transcodePromise = (async () => {
    const { buf } = await fetchAudioBuffer(upstreamUrl);
    const isOgg = buf.length >= 4 && buf[0] === 0x4f && buf[1] === 0x67 && buf[2] === 0x67 && buf[3] === 0x53;

    if (isOgg) {
      await fs.promises.writeFile(cachedFilePath, buf);
      return cachedFilePath;
    }

    const tempIn = path.join('/tmp', `taiko_in_${Date.now()}_${Math.random().toString(36).substring(2)}.tmp`);
    await fs.promises.writeFile(tempIn, buf);

    try {
      await new Promise<void>((resolve, reject) => {
        execFile(
          'ffmpeg',
          ['-y', '-i', tempIn, '-c:a', 'libvorbis', '-q:a', '4', '-f', 'ogg', cachedFilePath],
          (ffmpegErr) => {
            if (ffmpegErr) return reject(ffmpegErr);
            resolve();
          }
        );
      });
      return cachedFilePath;
    } finally {
      fs.unlink(tempIn, () => {});
    }
  })();

  inFlightTranscodes.set(safePathKey, transcodePromise);

  transcodePromise
    .then((filePath) => {
      inFlightTranscodes.delete(safePathKey);
      res.setHeader('Content-Type', 'audio/ogg');
      res.sendFile(filePath);
    })
    .catch((err) => {
      inFlightTranscodes.delete(safePathKey);
      console.warn('Error fetching or transcoding song:', req.path, err);
      next();
    });
});

app.use('/songs', createProxyMiddleware({ 
  target: 'https://taikoapp.uk', 
  changeOrigin: true,
  pathRewrite: (path, req) => '/songs' + path,
  on: {
    proxyRes: (proxyRes) => {
      proxyRes.headers['Access-Control-Allow-Origin'] = '*';
    }
  }
}))

app.use('/api/categories', categoriesRoutes)
app.use('/api/config', configRoutes)
app.use('/api/scores', scoresRoutes)
app.use('/api/songs', createProxyMiddleware({ 
  target: 'https://taikoapp.uk', 
  changeOrigin: true,
  pathRewrite: (path, req) => '/api/songs' + (path === '/' ? '' : path),
  on: {
    proxyRes: (proxyRes) => {
      proxyRes.headers['Access-Control-Allow-Origin'] = '*';
    }
  }
}))

const server = app.listen(PORT, '0.0.0.0', () => {
  console.log('Application listening at port ' + PORT)
})

gateway(server)
