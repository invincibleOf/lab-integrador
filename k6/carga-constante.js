// Carga constante para los experimentos de caos (2 req/s por defecto) sobre el borde (service-a)
import http from 'k6/http';
export const options = {
  scenarios: { steady: { executor: 'constant-arrival-rate', rate: Number(__ENV.RATE || 2), timeUnit: '1s',
                         duration: __ENV.DURATION || '30m', preAllocatedVUs: 20 } },
};
const orders = ['ord-001', 'ord-002', 'ord-003'];
export default function () {
  http.get(`http://${__ENV.HOST}:8000/order/${orders[Math.floor(Math.random() * 3)]}`);
}
